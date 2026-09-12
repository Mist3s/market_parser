"""Раскрутка коротких ссылок. Прямой егресс, проверка каждого хопа.

Почему раскрутка идёт БЕЗ прокси. После правила допуска каждый хоп — это
маркетплейсный хост, так что «не палить IP на шортенере» больше не аргумент.
Остался арифметический: проксированный хоп — егресс-событие на паре
(маркетплейс, прокси), то есть он берёт слот спейсинга. У Ozon интервал 5000
мс, а хоп стоит около 500, поэтому три проксированных хопа добавили бы
2 × 4500 мс гардов между собой плюс 4500 до первой ступени — 13 500 мс
обязательного ожидания против сетевого окна в 12 565. Лестницы при короткой
ссылке не существовало бы вовсе.

Безопасной раскрутку делает не выбор транспорта, а атрибутация егресса: всё,
что здесь наблюдается, помечается ``direct`` и здоровья прокси не касается.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from mktlink.constants import DIRECT_UNWIND_MS, REDIRECT_HOPS_MAX
from mktlink.timing.deadline import Deadline, stage
from mktlink.urls.registry import UnknownHost, match_path, rule_for_host, unwind_eligible
from mktlink.urls.ssrf import SsrfRejected, UnwindChallenged, check_path_veto, check_resolved
from mktlink.urls.validate import UrlRejected, validate

#: Читаем только статусную строку и Location: тело не нужно, а Range его и
#: не запрашивает. Отсюда и бюджет хопа в 150 мс на заголовки.
HOP_HEADERS: dict[str, str] = {"Range": "bytes=0-0", "Accept-Encoding": "identity"}

#: Только настоящие редиректы. 200 на шорт-форме означает, что нас увели на
#: страницу-заглушку, а не на товар.
REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


class HopFetcher(Protocol):
    """Один хоп. Возвращает (status, location)."""

    async def __call__(self, url: str, timeout_ms: int) -> tuple[int, str | None]: ...


class Resolver(Protocol):
    async def __call__(self, dl: Deadline, url: str, mp: str) -> tuple[str, int]: ...


class TooManyHops(Exception):
    """Цепочка длиннее потолка.

    Не датакласс: frozen-датакласс поверх Exception ломает присваивание
    ``__traceback__``, и исключение падает уже при распространении.
    """

    def __init__(self, url: str, hops: int) -> None:
        self.url = url
        self.hops = hops
        super().__init__(f"redirect chain exceeded {hops} hops at {url}")


class RedirectLoop(Exception):
    """Цепочка вернулась туда, где уже была."""


class CrossedMarketplace(Exception):
    """Хоп ушёл в другой маркетплейс. Заключение цепочки нарушено."""


class NotARedirect(Exception):
    """Шорт-форма ответила не редиректом."""


class RedirectResolver:
    """Раскрутчик. DNS и хоп инъектируются, поэтому тестируется без сети."""

    def __init__(
        self,
        fetch_hop: HopFetcher,
        resolve_dns: DnsResolver | None = None,
        *,
        max_hops: int = REDIRECT_HOPS_MAX,
    ) -> None:
        self._fetch = fetch_hop
        self._dns = resolve_dns
        self._max_hops = max_hops

    async def __call__(self, dl: Deadline, url: str, mp: str) -> tuple[str, int]:
        """Раскрутить до канонического URL. Возвращает (url, число хопов)."""
        started_ms = dl.elapsed_ms
        # 550 мс — оценка для планировщика, а не предел каждого живого соединения.
        # Весь путь, включая DNS, ограничен общим бюджетом раскрутки и запроса.
        async with stage(dl, "unwind", cap_ms=DIRECT_UNWIND_MS, reserve_ms=0) as budget_ms:
            return await self._walk(dl, url, mp, started_ms, budget_ms)

    async def _walk(self, dl: Deadline, url: str, mp: str,
                    started_ms: int, budget_ms: int) -> tuple[str, int]:
        visited: set[str] = set()
        current = url
        hops = 0

        # Терминал проверяется ПОСЛЕ каждого хопа, включая последний: цепочка
        # из max_hops переходов приводит к карточке, и не узнать её было бы
        # off-by-one, отвергающим ровно ту цепочку, ради которой потолок и
        # выбран.
        while True:
            parsed = validate(current)

            # Заключение в один маркетплейс: владеющая строка реестра
            # фиксируется на хопе 0 и не меняется.
            try:
                rule = rule_for_host(parsed.host)
            except UnknownHost as exc:
                raise CrossedMarketplace(f"hop left the allowlist: {parsed.host}") from exc
            if rule.marketplace != mp:
                raise CrossedMarketplace(
                    f"hop moved from {mp} to {rule.marketplace}: {parsed.host}"
                )

            # Челлендж приходит на ТОМ ЖЕ хосте, поэтому проверка хоста его
            # не ловит — нужен путевой veto.
            check_path_veto(mp, parsed.path, egress="direct", url=current)

            key = f"{parsed.host}{parsed.path}?{parsed.query}"
            if key in visited:
                raise RedirectLoop(current)
            visited.add(key)

            # Терминал: путь стал карточкой товара.
            m = match_path(parsed.host, parsed.path)
            if m.is_pdp:
                return current, hops

            if not unwind_eligible(parsed.host, parsed.path):
                raise NotARedirect(f"path is neither a product nor a short link: {parsed.path}")

            if hops >= self._max_hops:
                raise TooManyHops(current, hops)

            if self._dns is not None:
                check_resolved(await self._dns(parsed.host))

            remaining = max(0, budget_ms - (dl.elapsed_ms - started_ms))
            async with stage(dl, f"unwind{hops}", cap_ms=remaining, reserve_ms=0) as hop_ms:
                status, location = await self._fetch(current, hop_ms)

            if status in (403, 429):
                raise UnwindChallenged(current, egress="direct")
            if status not in REDIRECT_CODES or not location:
                raise NotARedirect(f"{current} answered {status}")

            current = _absolutise(current, location)
            hops += 1


class DnsResolver(Protocol):
    async def __call__(self, host: str) -> list[str]: ...


def _absolutise(base: str, location: str) -> str:
    from urllib.parse import urljoin  # noqa: PLC0415

    return urljoin(base, location)


def shortlink_key(url: str) -> str:
    """Ключ кэша раскрутки.

    Короткие ссылки неизменяемы, поэтому кэшируются надолго: раскрутка стоит
    до трёх хопов, и платить за неё дважды незачем.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


__all__ = [
    "CrossedMarketplace",
    "NotARedirect",
    "RedirectLoop",
    "RedirectResolver",
    "Resolver",
    "SsrfRejected",
    "TooManyHops",
    "UnwindChallenged",
    "UrlRejected",
    "shortlink_key",
]
