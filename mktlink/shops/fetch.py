"""Егресс для обычных магазинов: наш адрес напрямую, прокси — фолбэк.

ЗАМЕР 2026-09-18: все пятнадцать карточек с четырнадцати сайтов отвечают
``200`` с датацентрового адреса за 0.3–2.2 с под отпечатком Firefox
(``curl_cffi``, ``firefox147``). Ни капчи, ни челленджа, ни cookie не
потребовалось. Поэтому порядок здесь обратный маркетплейсному: прямой
запрос — основной путь, прокси берётся только когда прямой ответ похож на
блок и адрес в пуле вообще есть.

Редиректы следуются (до трёх): магазины уводят с ``http`` на ``https`` и
дописывают слеш. Куда в итоге пришли, отдаётся наружу — сервис проверяет, что
это всё ещё тот же магазин, а не главная страница вместо снятого товара.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from mktlink.egress.client import BodyTooLarge
from mktlink.timing.deadline import Deadline, DeadlineExceeded, stage

#: Потолок тела. Tilda-страница весит до 900 KiB, поэтому выше маркетплейсных
#: 2 MiB; выше трёх — уже не карточка.
MAX_BODY_BYTES: Final[int] = 3 * 1024 * 1024
#: Сколько редиректов проходим сами. Больше — не карточка, а петля.
MAX_REDIRECTS: Final[int] = 3

#: Статусы, при которых прямой ответ считается блоком и пробуется прокси.
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


class ShopUnreachable(Exception):
    """Сокет не открылся или соединение оборвалось. Не блок и не наш дедлайн."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    body: str
    #: Куда пришли после редиректов.
    final_url: str
    #: ``direct`` или ``proxy``.
    egress: str
    elapsed_ms: int
    #: Ответ похож на блок (статус или челлендж в теле).
    blocked: bool


class Transport(Protocol):
    """Один HTTP GET с редиректами. Инъектируется, чтобы тестировать без сети."""

    async def __call__(
        self, url: str, *, headers: dict[str, str], proxy: str | None, timeout_ms: int
    ) -> tuple[int, str, str]: ...


def looks_blocked(status: int, body: str) -> bool:
    if status in BLOCK_STATUSES:
        return True
    if status == 200 and len(body) <= CHALLENGE_BODY_MAX:
        low = body.casefold()
        return any(m in low for m in CHALLENGE_MARKERS)
    return False


class ShopFetcher:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        proxy_for: Callable[[], str | None] | None = None,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._transport = transport
        self._proxy_for = proxy_for or (lambda: None)
        self._max_bytes = max_bytes

    async def fetch(self, dl: Deadline, url: str, *, cap_ms: int, reserve_ms: int) -> Fetched:
        """Прямой запрос; при блоке — один повтор через прокси, если он есть.

        Стадии две и с разными именами: постмортем по леджеру должен видеть,
        что прямой путь получил блок и сколько стоил обход.
        """
        started = time.monotonic()
        status, body, final = await self._one(dl, url, "shop.fetch", None, cap_ms, reserve_ms)
        egress = "direct"
        blocked = looks_blocked(status, body)
        if blocked:
            proxy = self._proxy_for()
            if proxy:
                status, body, final = await self._one(
                    dl, url, "shop.fetch_proxy", proxy, cap_ms, reserve_ms
                )
                egress = "proxy"
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

    async def _one(
        self,
        dl: Deadline,
        url: str,
        stage_name: str,
        proxy: str | None,
        cap_ms: int,
        reserve_ms: int,
    ) -> tuple[int, str, str]:
        async with stage(dl, stage_name, cap_ms=cap_ms, reserve_ms=reserve_ms) as ms:
            return await self._send(url, proxy=proxy, timeout_ms=ms)

    async def _send(self, url: str, *, proxy: str | None, timeout_ms: int) -> tuple[int, str, str]:
        if self._transport is not None:
            return await self._transport(
                url, headers=dict(HEADERS), proxy=proxy, timeout_ms=timeout_ms
            )
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415
        from curl_cffi.requests.exceptions import RequestException, Timeout  # noqa: PLC0415

        try:
            async with AsyncSession(
                trust_env=False, impersonate=IMPERSONATE, max_redirects=MAX_REDIRECTS
            ) as s:
                r = await s.get(
                    url,
                    headers=HEADERS,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    timeout=timeout_ms / 1000,
                    allow_redirects=True,
                )
                return r.status_code, r.text, str(r.url)
        except Timeout:
            raise DeadlineExceeded("shop.fetch") from None
        except RequestException as exc:
            raise ShopUnreachable(type(exc).__name__.lower()) from exc


__all__ = [
    "BLOCK_STATUSES",
    "Fetched",
    "MAX_BODY_BYTES",
    "ShopFetcher",
    "ShopUnreachable",
    "Transport",
    "looks_blocked",
]
