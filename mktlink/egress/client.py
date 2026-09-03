"""Исходящий HTTP. curl_cffi с подделкой отпечатка Firefox.

Почему не httpx. Тёплый путь стоит на том, что cookie, снятые Camoufox,
переигрываются обычным клиентом. Это работает, только если клиент выглядит
тем же браузером — а httpx не подделывает ни JA3/JA4, ни HTTP/2 SETTINGS,
и подделать их не умеет в принципе. Это единственная причина зависимости.

**Ставка не проверена.** В удалённом скрейпере (``841dac3``) ``curl_cffi`` и
``impersonate`` встречаются ноль раз: он ходит браузером на каждый запрос.
Если реплей не работает, каждый запрос требует минтинга (p50 около 8.8 с), и
тогда бюджет в 15 секунд становится не опцией, а требованием, а один браузер
даёт потолок около 6 запросов в минуту. При заявленных 2–3 это всё ещё
работает, но с нулевым запасом. Метрика ``replay_ok_rate`` существует ровно
чтобы увидеть это сразу, а не волной таймаутов.

``trust_env=False`` везде: подхваченный из окружения ``HTTP_PROXY`` увёл бы
егресс мимо нашего адреса, и мы бы этого не заметили.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mktlink.egress.fingerprint import PROFILES, replay_headers
from mktlink.egress.jars import Jar
from mktlink.timing.deadline import Deadline, DeadlineExceeded, stage

#: Потолки тела. Больше не читаем: карточка столько не весит, а zip-бомба
#: весит сколько угодно.
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
#: Отношение распакованного к сжатому. У настоящей страницы это единицы,
#: у бомбы — тысячи.
MAX_DECOMPRESS_RATIO = 100


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    #: Через что сделано наблюдение. Скорер прокси читает только 'proxy'.
    egress: str
    elapsed_ms: int = 0


class Transport(Protocol):
    """Низкоуровневый запрос. Инъектируется, чтобы тестировать без сети."""

    async def __call__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        proxy: str | None,
        impersonate: str | None,
        timeout_ms: int,
    ) -> tuple[int, str]: ...


class BodyTooLarge(Exception):
    """Тело больше потолка. Читаем ограниченно и обрываем."""


class EgressClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport

    async def fetch(
        self,
        dl: Deadline,
        url: str,
        *,
        marketplace: str,
        cap_ms: int,
        reserve_ms: int,
        jar: Jar | None = None,
        proxy_url: str | None = None,
        stage_name: str | None = "fetch",
        max_bytes: int = MAX_HTML_BYTES,
    ) -> Response:
        """Один запрос внутри стадии бюджета.

        ``stage_name=None`` означает, что стадию УЖЕ открыл вызывающий, и
        открывать вторую нельзя. Вложенная стадия с тем же именем пишется в
        леджер дважды и удваивает время: замер по живой карточке WB показал
        ``[('wb.card_detail', 561), ('wb.card_detail', 562)]`` вместо одной
        строки. Функционально это безобидно — внутренний срез всё равно
        считается от остатка, — но постмортем и любой p99, выведенный из
        леджера, после такого врут.
        """
        profile = PROFILES[marketplace]
        headers = replay_headers(profile, jar.user_agent if jar else _fallback_ua(profile))
        if jar is not None:
            headers["Cookie"] = jar.cookie_header
        egress = "proxy" if proxy_url else "direct"

        if stage_name is None:
            ms = dl.slice_ms(cap_ms, reserve_ms)
            if ms <= 0:
                raise DeadlineExceeded("fetch")
            status, body = await self._send(
                url, headers=headers, proxy=proxy_url,
                impersonate=profile.impersonate, timeout_ms=ms,
            )
        else:
            async with stage(dl, stage_name, cap_ms=cap_ms, reserve_ms=reserve_ms) as ms:
                status, body = await self._send(
                    url, headers=headers, proxy=proxy_url,
                    impersonate=profile.impersonate, timeout_ms=ms,
                )
        if len(body) > max_bytes:
            raise BodyTooLarge(f"{len(body)} > {max_bytes}")
        return Response(status=status, body=body, egress=egress)

    async def _send(self, url: str, **kw: Any) -> tuple[int, str]:
        if self._transport is not None:
            return await self._transport(url, **kw)
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415

        async with AsyncSession(trust_env=False, impersonate=kw["impersonate"]) as s:
            r = await s.get(
                url,
                headers=kw["headers"],
                proxies={"http": kw["proxy"], "https": kw["proxy"]} if kw["proxy"] else None,
                timeout=kw["timeout_ms"] / 1000,
                allow_redirects=False,
            )
            return r.status_code, r.text


def _fallback_ua(profile: Any) -> str:
    """UA, когда jar'а нет вовсе — например, на прямой раскрутке.

    Форма минимальна намеренно: выдавать себя за конкретную сборку, cookie
    которой у нас нет, смысла не имеет.
    """
    return (
        f"Mozilla/5.0 (X11; Linux x86_64; rv:{profile.firefox_major}.0) "
        f"Gecko/20100101 Firefox/{profile.firefox_major}.0"
    )
