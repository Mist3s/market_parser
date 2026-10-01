"""Снятие челленджа QRATOR на Авито: proof-of-work по чистому HTTP, без браузера.

ЗАМЕР 2026-10-01, карточка 7822637882, прямой адрес без cookie:

1. ``GET`` карточки отвечает ``439`` с ``server: QRATOR`` и ставит cookie
   ``pow_challenge`` (``Max-Age=300``). В теле — страница «Доступ ограничен:
   проверка безопасности» со скриптом, который делает шаги 2–4 сам.
2. ``POST /web/3/firewallPow/get`` с ``{"challenge": <cookie>}`` отдаёт
   ``{"success": {"result": {"challenge_jwt": …, "max_solution_time_sec": 60}}}``
   и ставит cookie ``u``, ``v``, ``h_u``. Полезная нагрузка JWT —
   ``{compl, exp, iat, id, iss: "firewall-captcha", nbf, unblock_ttl_sec: 420}``.
3. Найти ``nonce``, при котором hex ``sha256(f"{id}:{nonce}")`` начинается с
   ``compl`` нулей. Во всех замерах ``compl = 3``: 500–4200 попыток, 1–7 мс.
4. ``POST /web/3/firewallPow/verify`` с ``{"challenge": <jwt>, "nonce": n}``
   отвечает ``{"success": {"result": {"unblock_ttl": 420, "verified": true}}}``.
5. Повторный ``GET`` той же сессией — ``200`` и карточка целиком (587 КБ).
   Cookie ``pow_solved``, которую ставит скрипт страницы, не нужна.

Холодное рукопожатие стоит 687–865 мс (четыре запроса), тёплый ``GET`` с
сохранёнными cookie — 314–470 мс.

**Почему своя сессия, а не :class:`~mktlink.egress.client.EgressClient`.**
Значение челленджа приходит ТОЛЬКО в ``Set-Cookie`` ответа ``439``, а транспорт
клиента возвращает ``(status, body)`` без заголовков и умеет только ``GET``.
Расширять общий контракт ради одного маркетплейса значило бы трогать три других
лейна и scrape.do. Поэтому здесь своя сессия curl_cffi — с тем же отпечатком
Firefox и тем же ``trust_env=False``.

**Почему разблокировка хранится в памяти процесса, а не в ``jar``.** Она
привязана к cookie, живёт 420 с и не переживает смену адреса; таблица ``jar``
вдобавок закрыта CHECK'ом ``mp IN ('ozon','wb','ym')``. Потерять её при
перезапуске стоит одного рукопожатия, то есть меньше секунды.

**Потолок сложности.** Python считает около 600 тысяч хэшей в секунду, и
ожидаемое число попыток — ``16 ** compl``: при ``compl = 5`` это уже ~1.7 с, при
``6`` — ~28 с. Выше :data:`MAX_COMPLEXITY` решение не начинается вовсе: это
честный отказ вместо гарантированного таймаута. Перебор идёт порциями и
отдаёт управление циклу событий: ``hashlib`` на коротком входе держит GIL, и
поток здесь ничего бы не дал.

Модуль не импортирует :mod:`mktlink.marketplaces` — направление зависимостей
обратное, — поэтому челлендж здесь опознаётся по статусу и cookie, а маркеры
тела знает :mod:`mktlink.marketplaces.avito`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from mktlink.egress.client import BodyTooLarge, fallback_ua
from mktlink.egress.fingerprint import FIREFOX, replay_headers
from mktlink.timing.deadline import Deadline, DeadlineExceeded

ORIGIN: Final[str] = "https://www.avito.ru"
GET_PATH: Final[str] = "/web/3/firewallPow/get"
VERIFY_PATH: Final[str] = "/web/3/firewallPow/verify"
#: То же число, что ``mktlink.marketplaces.avito.CHALLENGE_STATUS``; равенство
#: копий проверяет тест.
CHALLENGE_STATUS: Final[int] = 439
CHALLENGE_COOKIE: Final[str] = "pow_challenge"

#: Выше этого решение не начинается. См. докстроку модуля.
MAX_COMPLEXITY: Final[int] = 5
#: Попыток между возвратами управления циклу событий: около 17 мс работы.
SOLVE_CHUNK: Final[int] = 10_000
#: Запас до конца разблокировки. Запрос, начатый за секунду до истечения,
#: получил бы ``439`` и заплатил бы за рукопожатие посреди своего бюджета.
TTL_MARGIN_S: Final[int] = 60
#: Срок, если ответ ``verify`` его не назвал. Замерено: 420.
DEFAULT_UNBLOCK_TTL_S: Final[int] = 420

Cookie = tuple[str, str, str]  # имя, значение, домен


class PowFailed(Exception):
    """Рукопожатие не удалось. ``reason`` уходит в диагностику ступени."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TransportError(Exception):
    """Сеть не ответила: соединение сброшено, TLS не сошёлся."""


class UpstreamStatus(Exception):
    """Эндпоинт рукопожатия ответил ``429`` или ``5xx``.

    Это сбой или лимит, а не отказ в проверке, и вердикт у него тот же, что у
    такого же статуса самой карточки. Иначе пятисотка бэкенда челленджа
    выглядела бы для клиента как блок, с ретраем в 25 секунд вместо трёх.
    """

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(status)


@dataclass(frozen=True, slots=True)
class Unblock:
    """Снятый челлендж: cookie сессии и момент, после которого им не верим."""

    cookies: tuple[Cookie, ...]
    user_agent: str
    expires_at: float


class Session(Protocol):
    """HTTP-сессия с cookie. Инъектируется, чтобы тестировать без сети."""

    async def get(
        self, url: str, *, headers: dict[str, str], timeout_ms: int
    ) -> tuple[int, str]: ...

    async def post_json(
        self, url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout_ms: int
    ) -> tuple[int, str]: ...

    def cookie(self, name: str) -> str | None: ...

    def cookies(self) -> tuple[Cookie, ...]: ...

    def set_cookies(self, cookies: tuple[Cookie, ...]) -> None: ...

    async def aclose(self) -> None: ...


def jwt_payload(jwt: str) -> dict[str, Any]:
    """Полезная нагрузка JWT без проверки подписи: её проверяет сервер."""
    parts = jwt.split(".")
    if len(parts) != 3:
        raise PowFailed("bad_jwt")
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(seg))
    except ValueError:
        raise PowFailed("bad_jwt") from None
    if not isinstance(data, dict):
        raise PowFailed("bad_jwt")
    return data


async def solve(challenge_id: str, complexity: int, dl: Deadline, reserve_ms: int) -> int:
    """Найти ``nonce``. Порциями, с проверкой дедлайна между ними."""
    if complexity > MAX_COMPLEXITY:
        raise PowFailed("too_complex")
    prefix = "0" * complexity
    base = f"{challenge_id}:".encode()
    sha = hashlib.sha256
    nonce = 0
    while True:
        stop = nonce + SOLVE_CHUNK
        while nonce < stop:
            if sha(base + str(nonce).encode()).hexdigest().startswith(prefix):
                return nonce
            nonce += 1
        if dl.slice_ms(math.inf, reserve_ms) <= 0:
            raise DeadlineExceeded("avito.pow")
        await asyncio.sleep(0)


class Unblocker:
    """GET карточки с челленджем, снятым по ходу.

    Разблокировка кэшируется по адресу егресса: она привязана к cookie, а
    cookie — к адресу, с которого их получили.
    """

    def __init__(
        self,
        session_factory: Callable[[str | None], Session] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = session_factory or _CurlSession
        self._clock = clock
        self._cache: dict[int, Unblock] = {}

    def get(self, egress_id: int) -> Unblock | None:
        unblock = self._cache.get(egress_id)
        if unblock is not None and unblock.expires_at <= self._clock():
            del self._cache[egress_id]
            return None
        return unblock

    def forget(self, egress_id: int) -> None:
        self._cache.pop(egress_id, None)

    async def fetch(
        self,
        dl: Deadline,
        page_url: str,
        *,
        egress_id: int,
        proxy_url: str | None,
        cap_ms: int,
        reserve_ms: int,
        max_bytes: int,
    ) -> tuple[int, str]:
        """Страница карточки. Челлендж снимается внутри, если он пришёл.

        ``cap_ms`` — потолок на ВСЕ запросы вызова, а не на каждый: их до
        четырёх, и все идут внутри одной стадии ступени. Стадия держит потолок
        и сама, но таймаут транспорта, вышедший за него, честнее не выдавать.
        """
        window = _Window(dl, reserve_ms, time.monotonic() + cap_ms / 1000)
        cached = self.get(egress_id)
        ua = cached.user_agent if cached is not None else fallback_ua(FIREFOX)
        headers = replay_headers(FIREFOX, ua)
        session = self._factory(proxy_url)
        try:
            if cached is not None:
                session.set_cookies(cached.cookies)
            status, body = await session.get(
                page_url, headers=headers, timeout_ms=window.slice()
            )
            if status != CHALLENGE_STATUS:
                return status, _bounded(body, max_bytes)

            # Разблокировки не было или её отозвали раньше срока: в обоих
            # случаях старым cookie больше не верим.
            self.forget(egress_id)
            challenge = session.cookie(CHALLENGE_COOKIE)
            if not challenge:
                raise PowFailed("no_challenge")
            # Кэшируется сразу после ``verify``, а не после страницы: проверка
            # пройдена, и cookie действуют на сервере весь срок. Таймаут или
            # сброс на последнем GET иначе выбрасывал бы решённый челлендж, и
            # каждый повтор снова начинался бы с холодного рукопожатия.
            self._cache[egress_id] = await self._handshake(
                window, session, page_url, challenge, headers, ua
            )
            status, body = await session.get(
                page_url, headers=headers, timeout_ms=window.slice()
            )
            if status == CHALLENGE_STATUS:
                # Проверка пройдена, а страницу всё равно не отдали. Хранить
                # такую разблокировку — платить ею за следующий отказ.
                self.forget(egress_id)
            return status, _bounded(body, max_bytes)
        finally:
            await session.aclose()

    async def _handshake(
        self,
        window: _Window,
        session: Session,
        page_url: str,
        challenge: str,
        headers: dict[str, str],
        ua: str,
    ) -> Unblock:
        # Заголовки ``fetch()`` со страницы челленджа, а не навигации: тот же
        # браузер, который грузит документ, шлёт свои POST именно так.
        post_headers = {
            **headers,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": page_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        status, body = await session.post_json(
            ORIGIN + GET_PATH,
            {"challenge": challenge},
            headers=post_headers,
            timeout_ms=window.slice(),
        )
        _raise_for_upstream(status)
        result = _success_result(status, body)
        jwt = result.get("challenge_jwt") if result is not None else None
        if not isinstance(jwt, str) or not jwt:
            raise PowFailed("get_failed")

        claims = jwt_payload(jwt)
        cid, compl = claims.get("id"), claims.get("compl")
        if not isinstance(cid, (str, int)) or isinstance(cid, bool):
            raise PowFailed("bad_jwt")
        if not isinstance(compl, int) or isinstance(compl, bool) or compl < 0:
            raise PowFailed("bad_jwt")
        nonce = await solve(str(cid), compl, window.dl, window.reserve_ms)

        status, body = await session.post_json(
            ORIGIN + VERIFY_PATH,
            {"challenge": jwt, "nonce": nonce},
            headers=post_headers,
            timeout_ms=window.slice(),
        )
        _raise_for_upstream(status)
        result = _success_result(status, body)
        if result is None or result.get("verified") is not True:
            raise PowFailed("not_verified")

        ttl = _positive_int(result.get("unblock_ttl")) or _positive_int(
            claims.get("unblock_ttl_sec")
        ) or DEFAULT_UNBLOCK_TTL_S
        cookies = tuple(c for c in session.cookies() if c[0] != CHALLENGE_COOKIE)
        return Unblock(
            cookies=cookies,
            user_agent=ua,
            expires_at=self._clock() + max(0, ttl - TTL_MARGIN_S),
        )


@dataclass(frozen=True, slots=True)
class _Window:
    """Окно ступени: остаток её потолка, урезанный остатком дедлайна."""

    dl: Deadline
    reserve_ms: int
    stop_at: float

    def slice(self) -> int:
        left_ms = max(0.0, (self.stop_at - time.monotonic()) * 1000)
        ms = self.dl.slice_ms(left_ms, self.reserve_ms)
        if ms <= 0:
            raise DeadlineExceeded("avito.unblock")
        return ms


def _bounded(body: str, max_bytes: int) -> str:
    if len(body) > max_bytes:
        raise BodyTooLarge(f"{len(body)} > {max_bytes}")
    return body


def _raise_for_upstream(status: int) -> None:
    if status == 429 or status >= 500:
        raise UpstreamStatus(status)


def _success_result(status: int, body: str) -> dict[str, Any] | None:
    """``success.result`` ответа ``firewallPow`` или ``None``."""
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    success = data.get("success") if isinstance(data, dict) else None
    result = success.get("result") if isinstance(success, dict) else None
    return result if isinstance(result, dict) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


class _CurlSession:
    """Сессия curl_cffi: отпечаток Firefox, без окружения, без автоследования."""

    def __init__(self, proxy_url: str | None) -> None:
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415

        self._s = AsyncSession(trust_env=False, impersonate=FIREFOX.impersonate)
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    async def get(
        self, url: str, *, headers: dict[str, str], timeout_ms: int
    ) -> tuple[int, str]:
        return await self._request("GET", url, headers=headers, timeout_ms=timeout_ms)

    async def post_json(
        self, url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout_ms: int
    ) -> tuple[int, str]:
        return await self._request(
            "POST", url, headers=headers, timeout_ms=timeout_ms, json=payload
        )

    async def _request(
        self, method: str, url: str, *, headers: dict[str, str], timeout_ms: int, **kw: Any
    ) -> tuple[int, str]:
        from curl_cffi.requests.exceptions import RequestException, Timeout  # noqa: PLC0415

        try:
            r = await self._s.request(
                method,
                url,
                headers=headers,
                proxies=self._proxies,
                timeout=timeout_ms / 1000,
                allow_redirects=False,
                **kw,
            )
        except Timeout:
            raise DeadlineExceeded("avito.transport") from None
        except RequestException as exc:
            raise TransportError(str(exc)[:200]) from None
        return r.status_code, r.text

    def cookie(self, name: str) -> str | None:
        # Обход банки, а не ``cookies.get``: при одноимённых cookie на разных
        # доменах тот выбирает одну молча.
        for c in self._s.cookies.jar:
            if c.name == name and c.value:
                return c.value
        return None

    def cookies(self) -> tuple[Cookie, ...]:
        seen: dict[tuple[str, str], Cookie] = {}
        for c in self._s.cookies.jar:
            if c.value is not None:
                seen[(c.name, c.domain)] = (c.name, c.value, c.domain)
        return tuple(seen.values())

    def set_cookies(self, cookies: tuple[Cookie, ...]) -> None:
        for name, value, domain in cookies:
            self._s.cookies.set(name, value, domain=domain)

    async def aclose(self) -> None:
        await self._s.close()


#: Разблокировщик процесса. Один на процесс, потому что разблокировка — свойство
#: адреса, а адрес у процесса общий для всех запросов.
DEFAULT = Unblocker()
