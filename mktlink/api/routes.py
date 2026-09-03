"""Оркестратор пути запроса. Единственное место, где собирается ответ.

Порядок стадий фиксирован и обоснован: сперва всё, что не открывает сокет
(валидация, реестр, каноникализация, кэш), потом раскрутка редиректов прямым
егрессом, потом аренда jar и спейсинг, и только потом лестница извлечения.

Дешёвые отказы стоят миллисекунды и не тратят ни прокси, ни бюджет. Запрос
на маркетплейс вне скоупа отвечает 422 за единицы миллисекунд — при трёх
поддерживаемых маркетплейсах это самый частый отказ, а не экзотика.
"""

from __future__ import annotations

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
    SellerBlock,
    UrlBlock,
)
from mktlink.budget import BudgetTooSmall, plan
from mktlink.constants import BUDGET_FLOOR_MS
from mktlink.marketplaces.verdict import USABLE, SellerStatus, Verdict
from mktlink.timing.deadline import Deadline, DeadlineExceeded, bind, unbind
from mktlink.urls.canonical import Canonical, canonicalise
from mktlink.urls.registry import NotAProductUrl, UnknownHost, match_path
from mktlink.urls.ssrf import UnwindChallenged, looks_like_challenge_host
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
    def get(self, key: str) -> dict[str, Any] | None: ...
    def get_stale(self, key: str, max_age_s: int) -> tuple[dict[str, Any], int] | None: ...
    def put(self, key: str, value: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class Deps:
    """Зависимости оркестратора. Инъекция, чтобы путь запроса был тестируем."""

    cache: Cache
    ladder: Ladder
    resolver: Resolver | None = None
    budget_ms: int = 15_000
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


async def _run(
    req: ProductRequest, deps: Deps, dl: Deadline, rid: str, budget: int
) -> tuple[int, ProductResponse]:
    # --- стадии 1-3: чистый CPU, ни одного открытого сокета ------------------
    try:
        parsed = validate(req.url)
    except UrlRejected as exc:
        return _reject(exc.code, rid, req.url, detail={"reason": exc.detail})

    try:
        m = match_path(parsed.host, parsed.path)
    except UnknownHost:
        # Челлендж, уехавший на чужой хост, — это блок маркетплейса, а не
        # плохая ссылка. Отвечать 422 здесь значит врать клиенту.
        if looks_like_challenge_host(parsed.host):
            return _reject("unwind_challenged", rid, req.url)
        return _reject("host_not_allowed", rid, req.url, detail={"host": parsed.host})
    except NotAProductUrl as exc:
        return _reject(
            "not_a_product_url",
            rid,
            req.url,
            marketplace=exc.marketplace,
            detail={"classified": exc.classified} if exc.classified else {},
        )

    hops = 0

    # --- стадия 5: раскрутка, только для собственных шорт-форм ---------------
    if m.is_shortlink:
        if deps.resolver is None:
            return _reject("not_a_product_url", rid, req.url, marketplace=m.marketplace)
        try:
            resolved, hops = await deps.resolver(dl, req.url, m.marketplace)
        except UnwindChallenged:
            return _reject("unwind_challenged", rid, req.url, marketplace=m.marketplace)
        except DeadlineExceeded:
            # Канонический URL так и не получен — единственный случай, когда
            # 504 отдаётся без него.
            return _reject("deadline_exceeded", rid, req.url, marketplace=m.marketplace)
        parsed = validate(resolved)
        m = match_path(parsed.host, parsed.path)
        if not m.is_pdp:
            return _reject("not_a_product_url", rid, resolved, marketplace=m.marketplace)

    c = canonicalise(parsed.host, parsed.path, parsed.query, m)

    # --- стадия 4/6: кэш ------------------------------------------------------
    hit = deps.cache.get(c.cache_key)
    if hit is not None:
        return 200, _ok_from(hit, rid, req.url, c, hops, budget, dl, cache="hit")

    # --- лестница -------------------------------------------------------------
    try:
        plan(budget, c.marketplace, hops)
    except BudgetTooSmall:
        return _pending(
            rid, req.url, c, hops, budget, dl, reason="spacing_wait_exceeds_budget"
        )

    try:
        ex = await deps.ladder(dl, c, budget)
    except DeadlineExceeded:
        ex = Extraction(verdict=Verdict.BUDGET_EXHAUSTED, reason="deadline_exhausted")

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
        ),
    )
