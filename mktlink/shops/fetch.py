"""Егресс для обычных магазинов: наш адрес напрямую, обход — фолбэк.

ЗАМЕР 2026-09-18 с машины разработки (датацентр, Финляндия): все пятнадцать
карточек с четырнадцати сайтов отвечают ``200`` за 0.3–2.2 с под отпечатком
Firefox (``curl_cffi``, ``firefox147``). Ни капчи, ни челленджа, ни cookie.

ЗАМЕР 2026-09-18 с боевого VPS (датацентр, Амстердам): восемь хостов из
четырнадцати принимают TCP и НЕ отвечают на TLS Client Hello до самого
таймаута (moychay, moschaitorg, artoftea, chaline, imperatormin, aromatchaya,
tiptoptea, chaekshop). Это не гео и не отпечаток — с финского датацентра те
же хосты открываются обычным curl, а с VPS молчат и под Firefox, и под
Chrome. Это фильтр по сети VPS на стороне хостинга магазинов. Через scrape.do
с ``geoCode=ru`` (датацентровый адрес, 1 кредит) moychay отдаёт карточку
целиком за 1.2 с.

Отсюда схема из двух шагов:

1. **Прямой запрос** — основной путь: бесплатный и быстрый там, где работает.
   Фаза соединения (DNS + TCP + TLS) ограничена отдельно: честный хост
   проходит её за доли секунды, а молчание на TLS — признак фильтра, и ждать
   его весь бюджет значило бы не оставить времени на обход.
2. **Обход** — только если прямой путь молчит, оборвался или ответил блоком:
   прокси из пула, если он есть, иначе scrape.do. Один повтор, не больше.

Редиректы следуются (до трёх): магазины уводят с ``http`` на ``https`` и
дописывают слеш. Куда в итоге пришли, отдаётся наружу — сервис проверяет, что
это всё ещё тот же магазин, а не главная страница вместо снятого товара.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from mktlink.constants import SHOP_DIRECT_CONNECT_MS
from mktlink.egress.client import BodyTooLarge
from mktlink.egress.scrapedo import ALLOW_REDIRECTS, ScrapeDoError, interpret, shop_api_url
from mktlink.timing.deadline import Deadline, DeadlineExceeded, stage

#: Потолок тела. Tilda-страница весит до 900 KiB, поэтому выше маркетплейсных
#: 2 MiB; выше трёх — уже не карточка.
MAX_BODY_BYTES: Final[int] = 3 * 1024 * 1024
#: Сколько редиректов проходим сами. Больше — не карточка, а петля.
MAX_REDIRECTS: Final[int] = 3

#: Статусы, при которых прямой ответ считается блоком и пробуется обход.
BLOCK_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 429, 503})
#: Маркеры челленджа в ТЕЛЕ при статусе 200. Проверяются только на коротких
#: телах: у настоящей карточки в сотни KiB слово «captcha» встречается в
#: скрипте формы обратной связи, и это не блок.
CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "checking your browser",
    "attention required",
    "ddos-guard",
    "qrator",
    "showcaptcha",
    "доступ ограничен",
    "подозрительная активность",
)
CHALLENGE_BODY_MAX: Final[int] = 20_000

IMPERSONATE: Final[str] = "firefox147"
HEADERS: Final[dict[str, str]] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0"
    ),
}

#: Имена стадий в леджере: постмортем должен видеть, каким путём пришёл ответ.
STAGE_DIRECT: Final[str] = "shop.fetch"
STAGE_PROXY: Final[str] = "shop.fetch_proxy"
STAGE_API: Final[str] = "shop.fetch_api"


class ShopUnreachable(Exception):
    """Сокет не открылся, рукопожатие молчит или соединение оборвалось.

    Не блок и не наш дедлайн: ``connect_timeout`` — это хост, который принял
    TCP и не ответил на TLS за отведённую фазу соединения.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    body: str
    #: Куда пришли после редиректов.
    final_url: str
    #: ``direct``, ``proxy`` (адрес из пула) или ``api`` (scrape.do).
    egress: str
    elapsed_ms: int
    #: Ответ похож на блок (статус или челлендж в теле).
    blocked: bool


class Transport(Protocol):
    """Один HTTP GET с редиректами. Инъектируется, чтобы тестировать без сети."""

    async def __call__(
        self, url: str, *, headers: dict[str, str], proxy: str | None, timeout_ms: int
    ) -> tuple[int, str, str]: ...


class Route(Protocol):
    """Обходной путь чужими руками: ``(status, body, final_url)``."""

    async def __call__(self, url: str, *, timeout_ms: int) -> tuple[int, str, str]: ...


Send = Callable[[int], Awaitable[tuple[int, str, str]]]


def looks_blocked(status: int, body: str) -> bool:
    if status in BLOCK_STATUSES:
        return True
    if status == 200 and len(body) <= CHALLENGE_BODY_MAX:
        low = body.casefold()
        return any(m in low for m in CHALLENGE_MARKERS)
    return False


@dataclass(slots=True)
class ScrapeDoRoute:
    """Обход через scrape.do с российским датацентровым адресом (1 кредит).

    Не :class:`~mktlink.egress.scrapedo.ScrapeDoTransport`: тот привязан к
    маркетплейсам (закрытый список хостов, сокращатель, ``super``), а здесь
    хост уже проверен реестром или SSRF-фильтром сервиса, и нужен самый
    дешёвый тариф. Конечный URL берётся из заголовка поставщика, когда он
    его отдаёт; иначе считаем, что редиректов не было.
    """

    token: str
    #: Инъекция для тестов:
    #: ``(url, headers=..., timeout_ms=...) -> (status, body, resolved_url | None)``.
    sender: Any = None

    async def __call__(self, url: str, *, timeout_ms: int) -> tuple[int, str, str]:
        req = shop_api_url(url, token=self.token)
        try:
            status, body, resolved = await self._send(req, timeout_ms)
            status, body = interpret(status, body)
        except ScrapeDoError as exc:
            # Отказ поставщика — не ответ магазина и не плохая ссылка.
            raise ShopUnreachable(f"provider: {exc}") from exc
        return status, body, resolved or url

    async def _send(self, req: str, timeout_ms: int) -> tuple[int, str, str | None]:
        headers = dict(ALLOW_REDIRECTS)
        if self.sender is not None:
            return await self.sender(req, headers=headers, timeout_ms=timeout_ms)
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415
        from curl_cffi.requests.exceptions import RequestException, Timeout  # noqa: PLC0415

        try:
            async with AsyncSession(trust_env=False) as s:
                r = await s.get(req, headers=headers, timeout=timeout_ms / 1000)
        except Timeout:
            raise DeadlineExceeded(STAGE_API) from None
        except RequestException as exc:
            raise ShopUnreachable(f"provider_{type(exc).__name__.lower()}") from exc
        resolved = next(
            (v for k, v in r.headers.items() if k.lower() == "scrape.do-resolved-url"), None
        )
        return r.status_code, r.text, resolved


class ShopFetcher:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        proxy_for: Callable[[], str | None] | None = None,
        provider: Route | None = None,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._transport = transport
        self._proxy_for = proxy_for or (lambda: None)
        self._provider = provider
        self._max_bytes = max_bytes

    async def fetch(self, dl: Deadline, url: str, *, cap_ms: int, reserve_ms: int) -> Fetched:
        """Прямой запрос; если он молчит, оборвался или получил блок — один обход.

        Стадии носят разные имена: постмортем по леджеру должен видеть, что
        прямой путь не сработал и сколько стоил обход. Без обходного пути
        отказ прямого отдаётся как есть.
        """
        started = time.monotonic()
        egress = "direct"
        failure: Exception | None = None
        status, body, final = 0, "", url
        try:
            status, body, final = await self._within(
                dl, STAGE_DIRECT, cap_ms, reserve_ms, lambda ms: self._send(url, None, ms)
            )
        except (DeadlineExceeded, ShopUnreachable) as exc:
            failure = exc
        blocked = failure is None and looks_blocked(status, body)

        if failure is not None or blocked:
            detour = self._detour(url)
            if detour is None:
                if failure is not None:
                    raise failure
            else:
                egress, name, send = detour
                status, body, final = await self._within(dl, name, cap_ms, reserve_ms, send)
                blocked = looks_blocked(status, body)

        if len(body) > self._max_bytes:
            raise BodyTooLarge(f"{len(body)} > {self._max_bytes}")
        return Fetched(
            status=status,
            body=body,
            final_url=final or url,
            egress=egress,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            blocked=blocked,
        )

    def _detour(self, url: str) -> tuple[str, str, Send] | None:
        """Пул прокси в приоритете: он уже оплачен и не тратит кредиты."""
        proxy = self._proxy_for()
        if proxy:
            return "proxy", STAGE_PROXY, lambda ms: self._send(url, proxy, ms)
        provider = self._provider
        if provider is not None:
            return "api", STAGE_API, lambda ms: provider(url, timeout_ms=ms)
        return None

    @staticmethod
    async def _within(
        dl: Deadline, name: str, cap_ms: int, reserve_ms: int, send: Send
    ) -> tuple[int, str, str]:
        async with stage(dl, name, cap_ms=cap_ms, reserve_ms=reserve_ms) as ms:
            return await send(ms)

    async def _send(self, url: str, proxy: str | None, timeout_ms: int) -> tuple[int, str, str]:
        if self._transport is not None:
            return await self._transport(
                url, headers=dict(HEADERS), proxy=proxy, timeout_ms=timeout_ms
            )
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415
        from curl_cffi.requests.exceptions import RequestException, Timeout  # noqa: PLC0415

        # Фаза соединения отдельно от чтения: молчание на TLS должно стоить
        # три секунды, а не весь бюджет. Через прокси рукопожатие длиннее,
        # но тот же потолок: медленный прокси — тоже не путь.
        connect_ms = min(SHOP_DIRECT_CONNECT_MS, timeout_ms)
        read_ms = max(100, timeout_ms - connect_ms)
        started = time.monotonic()
        try:
            async with AsyncSession(
                trust_env=False, impersonate=IMPERSONATE, max_redirects=MAX_REDIRECTS
            ) as s:
                r = await s.get(
                    url,
                    headers=HEADERS,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    timeout=(connect_ms / 1000, read_ms / 1000),
                    allow_redirects=True,
                )
                return r.status_code, r.text, str(r.url)
        except Timeout:
            # Сорвалось задолго до конца отведённого времени — значит, на
            # фазе соединения: хост молчит. Это фильтр, а не наш дедлайн.
            if (time.monotonic() - started) * 1000 < timeout_ms - 500:
                raise ShopUnreachable("connect_timeout") from None
            raise DeadlineExceeded(STAGE_DIRECT) from None
        except RequestException as exc:
            raise ShopUnreachable(type(exc).__name__.lower()) from exc


__all__ = [
    "BLOCK_STATUSES",
    "CHALLENGE_MARKERS",
    "HEADERS",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "STAGE_API",
    "STAGE_DIRECT",
    "STAGE_PROXY",
    "Fetched",
    "Route",
    "ScrapeDoRoute",
    "ShopFetcher",
    "ShopUnreachable",
    "Transport",
    "looks_blocked",
]
