"""Оркестратор пути запроса. Единственное место, где собирается ответ.

Порядок стадий фиксирован и обоснован: сперва всё, что не открывает сокет
(валидация, реестр, каноникализация, кэш), потом раскрутка редиректов прямым
егрессом, потом аренда jar и спейсинг, и только потом лестница извлечения.

Дешёвые отказы стоят миллисекунды и не тратят ни прокси, ни бюджет. Запрос
на маркетплейс вне скоупа отвечает 422 за единицы миллисекунд — при трёх
поддерживаемых маркетплейсах это самый частый отказ, а не экзотика.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from mktlink.api.errors import http_for, retry_after_for
from mktlink.api.schemas import (
    MetaBlock,
    OfferBlock,
    ProductBlock,
    ProductRequest,
    ProductResponse,
    ResolveRequest,
    SellerBlock,
    UrlBlock,
)
from mktlink.budget import BudgetTooSmall, plan
from mktlink.constants import BUDGET_FLOOR_MS, RESOLVE_BUDGET_MS
from mktlink.egress.scrapedo import ScrapeDoError
from mktlink.marketplaces.verdict import USABLE, SellerStatus, Verdict
from mktlink.store.cache import FRESH_TTL_PINNED_S, FRESH_TTL_S
from mktlink.timing.deadline import Deadline, DeadlineExceeded, bind, unbind
from mktlink.urls.canonical import Canonical, canonicalise
from mktlink.urls.redirects import (
    CrossedMarketplace,
    NotARedirect,
    RedirectLoop,
    TooManyHops,
)
from mktlink.urls.registry import NotAProductUrl, UnknownHost, match_path
from mktlink.urls.ssrf import SsrfRejected, UnwindChallenged, looks_like_challenge_host
from mktlink.urls.validate import UrlRejected, validate


@dataclass(frozen=True, slots=True)
class Extraction:
    """Результат прохода по лестнице."""

    verdict: Verdict
    name: str | None = None
    seller_name: str | None = None
    seller_id: str | None = None
    seller_status: SellerStatus = SellerStatus.UNKNOWN_LAYOUT
    seller_source: str | None = None
    legal_name: str | None = None
    rung: str | None = None
    confirmations: int = 0
    reason: str | None = None


class Resolver(Protocol):
    """Раскрутка коротких ссылок. Прямой егресс, без прокси."""

    async def __call__(self, dl: Deadline, url: str, mp: str) -> tuple[str, int]: ...


class Ladder(Protocol):
    """Проход по лестнице извлечения."""

    async def __call__(self, dl: Deadline, c: Canonical, budget_ms: int) -> Extraction: ...


class Cache(Protocol):
    def get(
        self, key: str, *, fresh_ttl_s: int | None = None
    ) -> dict[str, Any] | None: ...
    def get_stale(self, key: str, max_age_s: int) -> tuple[dict[str, Any], int] | None: ...
    def put(self, key: str, value: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class Deps:
    """Зависимости оркестратора. Инъекция, чтобы путь запроса был тестируем."""

    cache: Cache
    ladder: Ladder
    resolver: Resolver | None = None
    #: Кэш раскрутки коротких ссылок. Без него раскрутка платится заново
    #: при каждом запросе той же ссылки — до трёх хопов по 550 мс из
    #: бюджета, хотя короткая ссылка неизменяема.
    unwound: Any = None
    #: Маркетплейсы, идущие через скрейпинг-API. Нужны ЗДЕСЬ, потому что
    #: предпроверка бюджета обязана проверять ту лестницу, по которой
    #: запрос реально пойдёт: у API-лейна полы 9000/4500 против 700/1100 у
    #: собственного егресса, и разница больше, чем весь бюджет 5 с.
    api_marketplaces: frozenset[str] = frozenset()
    budget_ms: int = 30_000
    #: Сроки свежести из НАСТРОЕК, а не из модуля кэша. Заказчик просил
    #: вынести их в конфиг, потому что срок будет расти; дефолты совпадают
    #: с константами, поэтому поведение без настройки не меняется.
    product_ttl_s: int = FRESH_TTL_S
    product_ttl_pinned_s: int = FRESH_TTL_PINNED_S
    ym_region_id: int = 213


async def handle(
    req: ProductRequest, deps: Deps, *, request_id: str | None = None
) -> tuple[int, ProductResponse]:
    """Обработать запрос. Возвращает HTTP-статус и тело единой формы."""
    rid = request_id or uuid.uuid4().hex
    budget = min(deps.budget_ms, req.max_wait_ms or deps.budget_ms)
    if budget < BUDGET_FLOOR_MS:
        return _reject(
            "invalid_budget", rid, req.url, detail={"floor_ms": BUDGET_FLOOR_MS}
        )

    dl = Deadline.start(budget - 400, rid)
    token = bind(dl)
    try:
        return await _run(req, deps, dl, rid, budget)
    finally:
        unbind(token)


class _Rejected(Exception):
    """Отказ, поднятый из общего опознания ссылки.

    Исключение, а не возврат кортежа: опознание вызывается из двух эндпоинтов
    с разными типами ответа, и «либо результат, либо готовый ответ» в двух
    местах читается хуже, чем один перехват.
    """

    def __init__(self, response: tuple[int, ProductResponse]) -> None:
        self.response = response
        super().__init__(response[1].status)


async def _identify(
    url: str, deps: Deps, dl: Deadline, rid: str
) -> tuple[Canonical, int, bool]:
    """Стадии 1–5: разобрать, опознать, при необходимости раскрутить.

    Общая часть двух эндпоинтов. ``/v1/resolve`` на этом заканчивается,
    ``/v1/product`` идёт дальше в кэш и лестницу.

    Третий элемент — БЫЛА ли раскрутка взята из кэша. Возвращается, а не
    выводится вызывающим: попытка вывести его из числа хопов дала прямую
    неправду — первый же ответ отрапортовал ``cache=hit`` на пустом кэше,
    потому что хопы больше нуля бывают и при живой раскрутке.
    """
    # Разбор и опознание применяются ДВА раза: к поданной ссылке и к тому, что
    # отдала раскрутка. Раньше второй раз шёл без обработки ошибок вовсе, и
    # короткая ссылка, ведущая на КАТЕГОРИЮ, роняла запрос: ``match_path``
    # бросает ``NotAProductUrl``, а не возвращает не-pdp совпадение, поэтому
    # проверка ``if not m.is_pdp`` до этого исключения не доживала. Клиент
    # получал 500 вместо 422. Общая функция закрывает это по построению: два
    # вызова одного кода не могут разойтись в обработке.
    def identify_one(candidate: str):
        try:
            p = validate(candidate)
        except UrlRejected as exc:
            raise _Rejected(
                _reject(exc.code, rid, candidate, detail={"reason": exc.detail})
            ) from None
        try:
            return p, match_path(p.host, p.path)
        except UnknownHost:
            # Челлендж, уехавший на чужой хост, — это блок маркетплейса, а не
            # плохая ссылка. Отвечать 422 здесь значит врать клиенту.
            if looks_like_challenge_host(p.host):
                raise _Rejected(_reject("unwind_challenged", rid, candidate)) from None
            raise _Rejected(
                _reject("host_not_allowed", rid, candidate, detail={"host": p.host})
            ) from None
        except NotAProductUrl as exc:
            raise _Rejected(
                _reject(
                    "not_a_product_url",
                    rid,
                    candidate,
                    marketplace=exc.marketplace,
                    detail={"classified": exc.classified} if exc.classified else {},
                )
            ) from None

    parsed, m = identify_one(url)
    hops = 0
    from_cache = False
    if m.is_shortlink:
        # Кэш раскрутки: короткая ссылка неизменяема, поэтому попадание здесь
        # экономит целые сетевые хопы из бюджета, а не микросекунды.
        cached = deps.unwound.get(url) if deps.unwound is not None else None
        if cached is not None:
            resolved, hops = cached.canonical, cached.hops
            from_cache = True
        else:
            if deps.resolver is None:
                raise _Rejected(
                    _reject("not_a_product_url", rid, url, marketplace=m.marketplace)
                ) from None
            try:
                resolved, hops = await deps.resolver(dl, url, m.marketplace)
            except ScrapeDoError as exc:
                response = _reject("capacity_exhausted", rid, url, marketplace=m.marketplace)
                response[1].meta.reason = exc.reason
                response[1].meta.degraded = True
                raise _Rejected(response) from None
            except UnwindChallenged:
                raise _Rejected(
                    _reject("unwind_challenged", rid, url, marketplace=m.marketplace)
                ) from None
            except DeadlineExceeded as exc:
                # Канонический URL так и не получен — единственный случай,
                # когда 504 отдаётся без него.
                raise _Rejected(
                    _reject("deadline_exceeded", rid, url, marketplace=m.marketplace,
                            detail={"stage": exc.stage_name, "elapsed_ms": dl.elapsed_ms})
                ) from None
            except SsrfRejected as exc:
                # Редирект увёл на приватный адрес. Это отказ ВХОДА: ссылка
                # ведёт туда, куда мы не ходим.
                raise _Rejected(
                    _reject(
                        "host_not_allowed",
                        rid,
                        url,
                        marketplace=m.marketplace,
                        detail={"reason": str(exc)[:200]},
                    )
                ) from None
            except (TooManyHops, RedirectLoop, CrossedMarketplace, NotARedirect) as exc:
                # Цепочка редиректов сломана: слишком длинная, циклическая,
                # уводящая на другой маркетплейс или отсутствующая вовсе.
                #
                # Раньше все четыре улетали в общий обработчик и отдавались как
                # 500 «capacity_exhausted». Для карточки это была экзотика, а
                # для /v1/resolve — ШТАТНЫЙ исход: сломанная короткая ссылка
                # есть нормальный вход эндпоинта, который её разворачивает.
                raise _Rejected(
                    _reject(
                        "not_a_product_url",
                        rid,
                        url,
                        marketplace=m.marketplace,
                        detail={"unwind": type(exc).__name__},
                    )
                ) from None
            if deps.unwound is not None:
                deps.unwound.put(url, resolved, hops)
        parsed, m = identify_one(resolved)
        if not m.is_pdp:
            # Раскрутка привела на нашу форму, но не на карточку: например
            # короткая ссылка на подборку. Это отказ входа, а не наша ошибка.
            raise _Rejected(
                _reject("not_a_product_url", rid, resolved, marketplace=m.marketplace)
            ) from None

    return canonicalise(parsed.host, parsed.path, parsed.query, m), hops, from_cache


async def resolve(
    req: ResolveRequest, deps: Deps, *, request_id: str | None = None
) -> tuple[int, ProductResponse]:
    """Короткая ссылка -> каноническая; название и продавца не извлекаем.

    С настроенным scrape.do переходы выполняет поставщик, включая рендеринг
    Ozon. Полный URL кэшируется без срока. До 30 с на первый запрос.
    """
    rid = request_id or uuid.uuid4().hex
    budget = min(RESOLVE_BUDGET_MS, req.max_wait_ms or RESOLVE_BUDGET_MS)
    dl = Deadline.start(budget - 100, rid)
    token = bind(dl)
    try:
        c, hops, unwind_cached = await _identify(req.url, deps, dl, rid)
    except _Rejected as exc:
        return exc.response
    finally:
        unbind(token)

    return 200, ProductResponse(
        status="ok",
        request_id=rid,
        url=UrlBlock(submitted=req.url, canonical=c.url, hops=hops),
        marketplace=c.marketplace,
        product=_product_block(c),
        # Продавца НЕ искали, и статус обязан говорить именно это. Дефолт
        # SellerBlock — 'unknown_layout', то есть «смотрели разметку и не
        # разобрались»: неудача разбора, которой здесь не было.
        seller=SellerBlock(status=str(SellerStatus.NOT_REQUESTED)),
        offer=_offer_block(c),
        meta=MetaBlock(
            source="resolve",
            budget_ms=budget,
            elapsed_ms=dl.elapsed_ms,
            ledger=list(dl.spent),
            # Честно: попадание в кэш РАСКРУТКИ, а не в продуктовый кэш.
            # На этом эндпоинте продуктового кэша нет вовсе.
            cache="hit" if unwind_cached else "miss",
        ),
    )


async def _run(
    req: ProductRequest, deps: Deps, dl: Deadline, rid: str, budget: int
) -> tuple[int, ProductResponse]:
    # --- стадии 1-5: разбор, опознание, раскрутка ----------------------------
    try:
        c, hops, _ = await _identify(req.url, deps, dl, rid)
    except _Rejected as exc:
        return exc.response

    # --- стадия 4/6: кэш ------------------------------------------------------
    # Срок свежести зависит от того, закреплён ли оффер в ссылке: на
    # закреплённом продавец — свойство ссылки, на модельном — снимок
    # аукциона. Разницу знает только этот уровень, поэтому он её и передаёт.
    fresh_ttl = deps.product_ttl_pinned_s if c.offer else deps.product_ttl_s
    # force_refresh обходит кэш ЦЕЛИКОМ, включая память процесса. Без этого
    # суточный срок свежести был бы односторонним: клиент мог разрешить старое,
    # но не мог потребовать свежего.
    hit = None if req.force_refresh else deps.cache.get(c.cache_key, fresh_ttl_s=fresh_ttl)
    if hit is not None:
        # Возраст сообщается и на ПОПАДАНИИ, а не только на stale. При сроке
        # 15 минут это было мелочью, при сутках — нет: ответ `ok, cache=hit`
        # без возраста не даёт клиенту узнать, вчерашние это данные или
        # секундные, и решить сам, устраивает ли его это.
        age = max(0, int(time.time() - float(hit.get("fetched_at") or 0)))
        return 200, _ok_from(
            hit, rid, req.url, c, hops, budget, dl, cache="hit", age_s=age
        )

    # --- лестница -------------------------------------------------------------
    via_api = c.marketplace in deps.api_marketplaces
    try:
        plan(budget, c.marketplace, hops, via_api=via_api)
    except BudgetTooSmall as exc:
        # Клиент САМ понизил бюджет ниже пола лейна — это ошибка входа, и
        # отвечать на неё «попробуй позже» значит обещать, что от повтора
        # что-то изменится. Не изменится: пол лейна постоянен.
        if req.max_wait_ms is not None:
            return _reject(
                "invalid_budget",
                rid,
                req.url,
                marketplace=c.marketplace,
                detail={"need_ms": exc.need, "have_ms": exc.have, "via_api": via_api},
            )
        # Бюджета не хватает при НАСТРОЙКЕ сервиса — это наша проблема, и
        # повтор осмыслен: лейн мог быть медленным временно.
        return _pending(
            rid, req.url, c, hops, budget, dl, reason="spacing_wait_exceeds_budget"
        )

    try:
        ex = await deps.ladder(dl, c, budget)
    except DeadlineExceeded:
        ex = Extraction(verdict=Verdict.BUDGET_EXHAUSTED, reason="deadline_exhausted")
    except ScrapeDoError as exc:
        ex = Extraction(verdict=Verdict.UPSTREAM_ERROR, reason=exc.reason)
    except BudgetTooSmall as exc:
        # Второй перехват — не паранойя, а закрытие проверенного дефекта.
        # Предпроверка выше смотрит ОДНУ лестницу, а лестница внутри может
        # оказаться другой (хопы, флаг рендера, смена транспорта). Раньше это
        # исключение улетало мимо handle(), который ловит только
        # DeadlineExceeded, и клиент получал 500 «capacity_exhausted» — то
        # есть «у сервиса кончилась ёмкость» вместо «ваш бюджет мал».
        return _reject(
            "invalid_budget",
            rid,
            req.url,
            marketplace=c.marketplace,
            detail={"need_ms": exc.need, "have_ms": exc.have},
        )

    if ex.verdict in USABLE and ex.name:
        body = _body_from_extraction(ex, c)
        deps.cache.put(c.cache_key, body)
        return 200, _ok_from(body, rid, req.url, c, hops, budget, dl, cache="miss", ex=ex)

    if ex.verdict is Verdict.NOT_FOUND:
        if ex.confirmations >= 2:
            return 404, _envelope(
                "not_found", rid, req.url, c, hops, budget, dl, confirmations=ex.confirmations
            )
        # Одного факта мало: молчаливый ноль — это форма блока, а не форма
        # отсутствия товара, и 404 без второго подтверждения был бы догадкой.
        return 200, _envelope(
            "not_found_unconfirmed", rid, req.url, c, hops, budget, dl, confirmations=1
        )

    # Отдать устаревшее лучше, чем не отдать ничего — но только если клиент
    # это разрешил и явно узнает возраст.
    if req.max_stale_s > 0:
        stale = deps.cache.get_stale(c.cache_key, req.max_stale_s)
        if stale is not None:
            body, age = stale
            resp = _ok_from(body, rid, req.url, c, hops, budget, dl, cache="stale")
            resp.status = "stale"
            resp.meta.degraded = True
            resp.meta.detail["age_s"] = age
            return 200, resp

    if ex.reason in ("provider_error", "provider_domain_disabled", "shortener_unavailable"):
        return 503, _envelope("capacity_exhausted", rid, req.url, c, hops, budget, dl,
                              reason=ex.reason, degraded=True)

    return _pending(
        rid, req.url, c, hops, budget, dl, reason=_reason_for(ex), verdict=ex.verdict
    )


#: Причина отказа, выведенная из вердикта. Дефолта «no_warm_jar» здесь быть
#: не может: лестница, отработавшая все ступени и получившая капчу, сообщала
#: бы клиенту «нет тёплой сессии» — то есть неверную причину, из-за которой он
#: стал бы ждать и повторять вместо того, чтобы узнать про блок.
_REASON_BY_VERDICT: dict[Verdict, str] = {
    Verdict.CAPTCHA: "marketplace_challenge",
    Verdict.HTTP_429: "marketplace_rate_limited",
    Verdict.UPSTREAM_ERROR: "marketplace_error",
    Verdict.SILENT_EMPTY: "marketplace_silent",
    Verdict.SCHEMA_DRIFT: "layout_changed",
    Verdict.CLIENT_RENDERED: "needs_render",
    Verdict.BUDGET_EXHAUSTED: "deadline_exhausted",
    Verdict.UNWIND_CHALLENGED: "unwind_challenged",
}


def _reason_for(ex: Extraction) -> str:
    """Явная причина от ступени важнее выведенной из вердикта."""
    if ex.reason:
        return ex.reason
    return _REASON_BY_VERDICT.get(ex.verdict, "unavailable")


# --- сборка ответов -----------------------------------------------------------


def _reject(
    status: str,
    rid: str,
    submitted: str,
    *,
    marketplace: str | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[int, ProductResponse]:
    return http_for(status), ProductResponse(
        status=status,  # type: ignore[arg-type]
        request_id=rid,
        url=UrlBlock(submitted=submitted),
        marketplace=marketplace,
        meta=MetaBlock(detail=detail or {}),
    )


def _envelope(
    status: str,
    rid: str,
    submitted: str,
    c: Canonical,
    hops: int,
    budget: int,
    dl: Deadline,
    **meta: Any,
) -> ProductResponse:
    return ProductResponse(
        status=status,  # type: ignore[arg-type]
        request_id=rid,
        url=UrlBlock(submitted=submitted, canonical=c.url, hops=hops),
        marketplace=c.marketplace,
        product=_product_block(c),
        offer=_offer_block(c),
        meta=MetaBlock(
            budget_ms=budget,
            elapsed_ms=dl.elapsed_ms,
            ledger=list(dl.spent),
            **meta,
        ),
    )


def _pending(
    rid: str,
    submitted: str,
    c: Canonical,
    hops: int,
    budget: int,
    dl: Deadline,
    *,
    reason: str,
    verdict: Verdict | None = None,
) -> tuple[int, ProductResponse]:
    resp = _envelope(
        "pending", rid, submitted, c, hops, budget, dl, reason=reason
    )
    resp.meta.retry_after_seconds = retry_after_for(reason)
    if verdict is not None:
        resp.meta.detail["verdict"] = str(verdict)
    return 202, resp


def _product_block(c: Canonical, name: str | None = None) -> ProductBlock:
    ids = c.ids
    kind = next((k for k in ("sku", "nm", "sku_id", "product_id", "ware_md5") if k in ids), None)
    return ProductBlock(
        id=ids.get(kind) if kind else None,
        id_kind=kind,
        product_id=ids.get("product_id"),
        sku_id=ids.get("sku_id") or ids.get("sku") or ids.get("nm"),
        name=name,
    )


def _offer_block(c: Canonical) -> OfferBlock:
    if c.offer:
        return OfferBlock(
            ref="&".join(f"{k}={v}" for k, v in c.offer),
            selection="explicit",
            stable=True,
        )
    return OfferBlock(ref=None, selection="marketplace_default", stable=False)


def _body_from_extraction(ex: Extraction, c: Canonical) -> dict[str, Any]:
    return {
        "name": ex.name,
        "seller_name": ex.seller_name,
        "seller_id": ex.seller_id,
        "seller_status": str(ex.seller_status),
        "seller_source": ex.seller_source,
        "legal_name": ex.legal_name,
        "rung": ex.rung,
        "canonical": c.url,
    }


def _ok_from(
    body: dict[str, Any],
    rid: str,
    submitted: str,
    c: Canonical,
    hops: int,
    budget: int,
    dl: Deadline,
    *,
    cache: str,
    ex: Extraction | None = None,
    age_s: int | None = None,
) -> ProductResponse:
    status = body.get("seller_status", str(SellerStatus.UNKNOWN_LAYOUT))
    resolved = status in (str(SellerStatus.RESOLVED), str(SellerStatus.FIRST_PARTY))
    return ProductResponse(
        status="ok",
        request_id=rid,
        url=UrlBlock(submitted=submitted, canonical=c.url, hops=hops),
        marketplace=c.marketplace,
        product=_product_block(c, body.get("name")),
        seller=SellerBlock(
            # Имя отдаётся ТОЛЬКО при разрешённом статусе. При любом другом
            # это null, и никогда не бренд и не имя маркетплейса.
            name=body.get("seller_name") if resolved else None,
            # Найденный, но не привязанный к офферу продавец. Уходит в
            # отдельное поле, чтобы наивное чтение ``name`` не могло записать
            # продавца чужого оффера — см. докстроку SellerBlock.
            unverified_name=(
                body.get("seller_name")
                if status == str(SellerStatus.CARD_DEFAULT)
                else None
            ),
            id=body.get("seller_id"),
            legal_name=body.get("legal_name"),
            kind=(
                "first_party"
                if status == str(SellerStatus.FIRST_PARTY)
                else ("third_party" if resolved else "unknown")
            ),
            status=status,
            source=body.get("seller_source"),
        ),
        offer=_offer_block(c),
        meta=MetaBlock(
            source="replay",
            rung=body.get("rung"),
            degraded=not resolved,
            confirmations=ex.confirmations if ex else 1,
            budget_ms=budget,
            elapsed_ms=dl.elapsed_ms,
            cache=cache,  # type: ignore[arg-type]
            ledger=list(dl.spent),
            # Возраст на попадании. В том же поле, что у stale-ответа, а не
            # в новом: клиент читает возраст одинаково независимо от того,
            # свежие данные или разрешённые старые.
            detail={"age_s": age_s} if age_s is not None else {},
        ),
    )
