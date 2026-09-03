"""Минтинг cookie-jar. Живёт в forge и НИКОГДА не на пути запроса.

Стоимость по замерам репозитория: запуск Camoufox 2–8 с, ``page.goto``
0.5–3 с, захардкоженное ожидание прогрева 6 с (``ozon.py:95``), навигация
0.6–2 с. Итого 9–19 с — больше, чем весь бюджет ответа при любой настройке
ручки. Отсюда и разделение процессов: это не архитектурная эстетика, а
арифметика.

Самый ценный измеряемый тюнабл — те самые 6 секунд. В репозитории они стоят
для КАТЕГОРИЙНОЙ страницы; нужны ли они в том же объёме, чтобы получить
cookie для карточки, никто не мерил. Сокращение вдвое сняло бы 3 секунды с
p50 минтинга.

Публикация jar отдельным шагом ПОСЛЕ проверки реплея. Минтинг мог
завершиться, но вся ставка тёплого пути — в том, что снятые cookie
переигрываются через ``curl_cffi``, и до подтверждения этого jar читателю не
виден.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from mktlink.egress.fingerprint import PROFILES, check_mint
from mktlink.egress.jars import Jar, JarStore
from mktlink.marketplaces import ym
from mktlink.timing.deadline import assert_off_request_path

#: [репо] ozon.py:95 — прогрев категории. Объявлен ИЗМЕРЯЕМЫМ тюнаблом:
#: значение взято из батч-скрейпера, где оно грело листинг, а не карточку.
WARMUP_MS: dict[str, int] = {"ozon": 6000, "wb": 5000, "ym": 1500}

#: Страницы прогрева. Cookie ставятся при заходе на человеческую страницу,
#: а не при обращении к API — именно поэтому прогрев вообще нужен.
WARMUP_URL: dict[str, str] = {
    "ozon": "https://www.ozon.ru/",
    "wb": "https://www.wildberries.ru/",
    "ym": f"https://market.yandex.ru/?lr={ym.REGION_ID}",
}

#: Cookie, без которых jar бесполезен. Пустое множество означает «любые».
REQUIRED_COOKIES: dict[str, frozenset[str]] = {
    "ozon": frozenset({"__Secure-ETC"}),
    "wb": frozenset(),
    "ym": frozenset({"yandexuid"}),
}


class Browser(Protocol):
    """Минимум, который нужен от браузера. Реализация живёт в forge."""

    async def cookies_for(self, url: str, *, proxy_url: str, wait_ms: int) -> tuple[str, str]:
        """Вернуть (cookie_header, user_agent)."""
        ...


class ReplayCheck(Protocol):
    async def __call__(self, jar: Jar, proxy_url: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class MintResult:
    ok: bool
    marketplace: str
    proxy_id: int
    elapsed_ms: int
    detail: str = ""


async def mint(
    browser: Browser,
    jars: JarStore,
    *,
    marketplace: str,
    proxy_id: int,
    proxy_url: str,
    verify_replay: ReplayCheck,
) -> MintResult:
    """Снять jar, проверить реплей, опубликовать.

    Порядок шагов — гарантия, а не стиль: непроверенный jar записывается,
    но не публикуется, поэтому падение между шагами оставляет систему без
    тёплой сессии, а не с сессией, которая молча не работает.
    """
    assert_off_request_path(f"mint {marketplace}")
    started = time.monotonic()
    profile = PROFILES[marketplace]

    cookie_header, user_agent = await browser.cookies_for(
        WARMUP_URL[marketplace], proxy_url=proxy_url, wait_ms=WARMUP_MS[marketplace]
    )

    # Сверка связывает сборку браузера с профилем реплея. Пинится только
    # мажор: Camoufox ротирует остальное на каждый запуск, и сравнение
    # полного UA падало бы на большинстве минтингов.
    check_mint(user_agent, profile)

    required = REQUIRED_COOKIES.get(marketplace, frozenset())
    if required and not all(c in cookie_header for c in required):
        return MintResult(
            False,
            marketplace,
            proxy_id,
            _ms(started),
            f"missing required cookies: {sorted(required)}",
        )

    if marketplace == "ym":
        cookie_header = _force_region(cookie_header)

    jar = Jar(
        marketplace=marketplace,
        proxy_id=proxy_id,
        cookie_header=cookie_header,
        user_agent=user_agent,
        firefox_major=profile.firefox_major,
        minted_at=int(time.time()),
    )
    jars.put_unverified(jar)

    if not await verify_replay(jar, proxy_url):
        # Cookie сняты, но через curl_cffi не работают. Это отказ ГЛАВНОЙ
        # ставки дизайна, и он обязан быть видим метрикой, а не волной
        # таймаутов через три недели.
        return MintResult(False, marketplace, proxy_id, _ms(started), "replay verification failed")

    jars.publish(marketplace, proxy_id)
    return MintResult(True, marketplace, proxy_id, _ms(started))


def _force_region(cookie_header: str) -> str:
    """Переписать регион в cookie.

    Регион форсируется и в query (``lr=213``), и здесь: если Яндекс возьмёт
    его из cookie, а мы этого не сделаем, ответ придёт для региона, куда
    геолоцируется прокси, — и цена с наличием будут не те, что мы обещали.
    """
    parts = [p.strip() for p in cookie_header.split(";") if p.strip()]
    kept = [p for p in parts if not p.startswith("yandex_gid=")]
    kept.append(f"yandex_gid={ym.REGION_ID}")
    return "; ".join(kept)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def estimate_eta_ms(marketplace: str) -> int:
    """Оценка длительности для кадра прогресса.

    Нужна не для красоты: из неё считается ``Retry-After``, а тот определяет,
    попадёт клиент повтором в готовый кэш или получит второй ``202``.
    """
    launch = 5000  # середина наблюдённого репозиторием диапазона 2–8 с
    return launch + WARMUP_MS[marketplace] + 2000


__all__ = [
    "REQUIRED_COOKIES",
    "WARMUP_MS",
    "WARMUP_URL",
    "Browser",
    "MintResult",
    "estimate_eta_ms",
    "mint",
]
