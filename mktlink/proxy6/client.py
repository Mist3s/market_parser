"""Клиент proxy6.net. Единственный вход в API, и он никогда не на пути запроса.

Три свойства, каждое из которых обязательно:

1. **Ключ в PATH, а не в query.** Так устроен API: ``/api/{key}/{method}/``.
   Значит ключ попадает в любую строку URL, которую мы залогируем, поэтому
   :func:`_redact` вырезает его из всех сообщений об ошибках.
2. **Жёсткий потолок 3 rps.** Ограничиваем себя сами до 2.5 rps семафором на
   одну операцию плюс минимальный зазор. Ограничитель процесс-локальный, и это
   корректно **только** при единственном писателе; единственность обеспечена
   flock'ом в forge, и если он потерян — писателя два, ограничитель врёт.
3. **``assert_off_request_path`` первой строкой.** Вызов proxy6 внутри запроса
   означал бы секунды в очереди внутри бюджета. Это единственная точка входа,
   поэтому гард здесь полон, в отличие от гарда на браузер.

Чего клиент НЕ делает: ретраев ``buy``. Повторить закупку нельзя — вместо
этого потерянный ответ разбирается по nonce в ``descr`` (см. :mod:`.descr`).
``prolong`` тоже не ретраится: свидетелем служит ``term_end`` до операции.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

from mktlink.proxy6.errors import raise_for
from mktlink.timing.deadline import assert_off_request_path

API_BASE: Final[str] = "https://px6.link/api"

#: Жёсткий потолок API — 3 rps. Идём на 2.5, чтобы дрожание сети не выносило
#: нас за границу: 429 от proxy6 означает нарушение инварианта, а не «бывает».
MIN_GAP_S: Final[float] = 0.40

#: Методы, которые нельзя повторять при потерянном ответе: повтор стоит денег.
NON_IDEMPOTENT: Final[frozenset[str]] = frozenset({"buy", "prolong"})


@dataclass(frozen=True, slots=True)
class ProxyRow:
    """Строка из ``getproxy``. Поля — ровно те, что отдаёт API.

    Обратите внимание, чего здесь НЕТ: ни ``state``, ни ``auto_prolong``.
    Состояние живёт в нашем сторе, а включённость автопродления API не
    показывает вовсе — её приходится выводить поведенчески, по прыжку
    ``unixtime_end`` без нашего ``prolong``.
    """

    id: int
    version: int
    ip: str
    host: str
    port: int
    user: str
    password: str
    type: str
    country: str
    date: str
    date_end: str
    unixtime: int
    unixtime_end: int
    descr: str
    active: bool

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> ProxyRow:
        return cls(
            id=int(d["id"]),
            version=int(d["version"]),
            ip=str(d["ip"]),
            host=str(d["host"]),
            port=int(d["port"]),
            user=str(d["user"]),
            password=str(d["pass"]),
            type=str(d.get("type", "http")),
            country=str(d.get("country", "")),
            date=str(d.get("date", "")),
            date_end=str(d.get("date_end", "")),
            unixtime=int(d.get("unixtime", 0)),
            unixtime_end=int(d.get("unixtime_end", 0)),
            descr=str(d.get("descr", "")),
            active=str(d.get("active", "1")) in ("1", "true", "True"),
        )

    def proxy_url(self) -> str:
        return f"http://{self.user}:{self.password}@{self.host}:{self.port}"


class Proxy6Client:
    """Асинхронный клиент. Одна операция в полёте, зазор между операциями."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Any | None = None,
        country: str = "ru",
    ) -> None:
        if not api_key:
            raise ValueError("proxy6 api key is required")
        self._key = api_key
        self._country = country
        # Семафор(1): не «оптимизация», а условие корректности процесс-локального
        # ограничителя. Две одновременные операции считали бы зазор независимо.
        self._gate = asyncio.Semaphore(1)
        self._last_call = 0.0
        self._transport = transport

    # --- транспорт ----------------------------------------------------------

    async def _http_get(self, url: str) -> dict[str, Any]:
        if self._transport is not None:
            return await self._transport(url)
        # Импорт внутри функции: в образе api этого модуля не будет вовсе,
        # и падать он должен на импорте клиента, а не на импорте пакета.
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415

        async with AsyncSession(trust_env=False) as s:
            r = await s.get(url, timeout=30)
            return r.json()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Вызвать метод API. Единственный путь наружу."""
        assert_off_request_path(f"proxy6.{method}")
        url = self._url(method, params)
        async with self._gate:
            gap = MIN_GAP_S - (time.monotonic() - self._last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                payload = await self._http_get(url)
            finally:
                self._last_call = time.monotonic()
        if payload.get("status") != "yes":
            raise_for(int(payload.get("error_id", 30)), method)
        return payload

    def _url(self, method: str, params: dict[str, Any]) -> str:
        from urllib.parse import urlencode  # noqa: PLC0415

        clean = {k: v for k, v in params.items() if v is not None}
        qs = f"?{urlencode(clean)}" if clean else ""
        return f"{API_BASE}/{self._key}/{method}/{qs}"

    # --- методы -------------------------------------------------------------

    async def getprice(self, *, count: int, period: int, version: int) -> dict[str, Any]:
        return await self.call("getprice", count=count, period=period, version=version)

    async def getcount(self, *, version: int, country: str | None = None) -> int:
        p = await self.call("getcount", country=country or self._country, version=version)
        return int(p.get("count", 0))

    async def getproxy(
        self,
        *,
        state: str = "all",
        descr: str | None = None,
        page: int = 1,
        limit: int = 1000,
    ) -> list[ProxyRow]:
        p = await self.call("getproxy", state=state, descr=descr, page=page, limit=limit)
        raw = p.get("list") or {}
        # API отдаёт list либо словарём по id, либо массивом — принимаем оба.
        items = raw.values() if isinstance(raw, dict) else raw
        return [ProxyRow.from_api(d) for d in items]

    async def buy(
        self,
        *,
        count: int,
        period: int,
        version: int,
        descr: str,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Купить. ``auto_prolong`` НЕ ПЕРЕДАЁТСЯ НИКОГДА.

        Причина не в осторожности: API не умеет его выключить после покупки,
        поэтому единственный способ сохранить контроль над продлением — не
        включать его вовсе. Продление становится нашим действием, и «никогда
        не продлевать» превращается в «не делать действие».

        Повторов у этого метода нет и быть не может. Потерянный ответ
        разбирается по nonce в ``descr``.
        """
        return await self.call(
            "buy",
            count=count,
            period=period,
            country=country or self._country,
            version=version,
            descr=descr,
        )

    async def prolong(self, *, period: int, ids: list[int]) -> dict[str, Any]:
        """Продлить. Тоже без повторов: свидетель — ``term_end`` до операции."""
        return await self.call("prolong", period=period, ids=",".join(map(str, ids)))

    async def setdescr(
        self, *, new: str, ids: list[int] | None = None, old: str | None = None
    ) -> int:
        """Переписать тэг. Ровно один из ``ids`` / ``old``.

        Чужие прокси сюда не попадают никогда: namespace guard проверяется
        вызывающим до этой точки, потому что переименование чужого адреса —
        захват оплаченной кем-то собственности.
        """
        if (ids is None) == (old is None):
            raise ValueError("setdescr needs exactly one of ids / old")
        if len(new) > 50:
            raise ValueError(f"descr exceeds proxy6 limit of 50 chars: {len(new)}")
        p = await self.call(
            "setdescr",
            new=new,
            ids=",".join(map(str, ids)) if ids else None,
            old=old,
        )
        return int(p.get("count", 0))

    async def delete(self, *, ids: list[int] | None = None, descr: str | None = None) -> int:
        if (ids is None) == (descr is None):
            raise ValueError("delete needs exactly one of ids / descr")
        p = await self.call(
            "delete", ids=",".join(map(str, ids)) if ids else None, descr=descr
        )
        return int(p.get("count", 0))

    async def check(self, *, proxy_id: int) -> bool:
        """Проверка живости ГЛАЗАМИ proxy6.

        Достижимость маркетплейса этим не проверяется вообще — отсюда
        отдельная валидационная лестница при приёмке нового адреса.
        """
        p = await self.call("check", ids=proxy_id)
        return bool(p.get("proxy_status"))


def redact(text: str, api_key: str) -> str:
    """Вырезать ключ из строки перед логированием.

    Ключ сидит в PATH, поэтому попадает в любой URL, который мы напечатаем.
    """
    return text.replace(api_key, "***") if api_key else text
