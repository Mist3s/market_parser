"""Согласованность отпечатка. Одно семейство: Camoufox — это Firefox.

Ставка, на которой стоит весь тёплый путь: cookie, снятые Camoufox, можно
переиграть обычным HTTP-клиентом. Она держится ровно до тех пор, пока
переигрывающий клиент выглядит тем же браузером, что и минтивший.

Camoufox основан на Firefox, значит ``impersonate`` в curl_cffi обязан быть
Firefox-профилем. Chrome-UA без ``sec-ch-ua`` поверх Firefox-образного
TLS-хендшейка — бесплатный тэлл для любого edge, который вообще смотрит.

Обычный ``httpx`` здесь непригоден в принципе: он не подделывает ни JA3/JA4,
ни HTTP/2 SETTINGS. Без этого тёплого пути не существует, и это единственная
причина тащить в зависимости ``curl_cffi``.

**User-Agent не замораживается, и это исправление, а не упрощение.** Репозиторный
``_camoufox_launch_kwargs`` не передаёт ``os=``, поэтому Camoufox ротирует
отпечаток при каждом запуске, включая OS-токен в UA. Сравнение полного UA с
замороженной строкой падало бы на большинстве минтингов, и jar не публиковался
бы вовсе.

**Мажоры тоже не совпадают, и требовать равенства нельзя.** ЗАМЕР 2026-09-03:
установленный Camoufox v152.0.4-beta.29 — это Firefox **152**, а самая новая
цель curl_cffi 0.16.3 — **firefox147**. Разрыв в пять версий, и он структурный:
у двух инструментов независимые циклы релизов, поэтому «пинить мажор и
сверять на равенство» — условие, которое не выполнится никогда. Первая
редакция этого модуля требовала равенства и заблокировала бы минтинг целиком.

Что проверяется вместо равенства:

1. Подделываемый мажор НЕ НОВЕЕ браузерного. Выдавать TLS-отпечаток версии,
   которой ещё нет, — это тэлл наоборот, хуже отставания.
2. Разрыв в пределах названного допуска. Firefox не меняет ни TLS-профиль, ни
   HTTP/2 SETTINGS каждую версию, поэтому небольшое отставание безобидно; но
   допуск должен быть числом в коде, а не надеждой.
3. Наблюдённый мажор пишется рядом с jar, чтобы дрейф был виден метрикой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_FF_MAJOR: Final[re.Pattern[str]] = re.compile(r"Firefox/(\d+)")


class FingerprintDrift(RuntimeError):
    """Сборка браузера разошлась с профилем реплея.

    Ломает сборку сразу, а не success rate через три недели.
    """


@dataclass(frozen=True, slots=True)
class FingerprintProfile:
    """Связка «чем минтим» и «чем переигрываем»."""

    name: str
    minter: str  # camoufox
    impersonate: str  # curl_cffi, форма firefox<major>
    firefox_major: int
    accept_language: str
    #: Firefox их не шлёт. Chrome шлёт. Несоответствие — тэлл.
    sends_sec_ch_ua: bool

    def __post_init__(self) -> None:
        want = self.impersonate.removeprefix("firefox")
        if not want.isdigit() or int(want) != self.firefox_major:
            raise ValueError(
                f"{self.name}: impersonate {self.impersonate!r} does not match "
                f"firefox_major {self.firefox_major}"
            )
        if self.minter == "camoufox" and self.sends_sec_ch_ua:
            raise ValueError(f"{self.name}: Firefox does not send sec-ch-ua")


#: Максимальный разрыв мажоров, который считаем безопасным.
#: Восемь версий Firefox — это примерно год релизов, и внутри года
#: TLS-профиль стабилен. Число здесь важнее его точности: без него допуск
#: становится «сколько получится».
MAX_MAJOR_GAP: Final[int] = 8

#: Цели curl_cffi 0.16.3. Список нужен, чтобы выбирать ближайшую доступную,
#: а не назначать её вручную при каждом обновлении браузера.
AVAILABLE_FIREFOX_TARGETS: Final[tuple[int, ...]] = (133, 135, 144, 147)

#: Один профиль на все три маркетплейса: минтит везде Camoufox.
#: ``impersonate`` — самая новая доступная цель, а не «та же, что у браузера»:
#: одинаковой не бывает (см. докстроку модуля).
FIREFOX = FingerprintProfile(
    name="ff",
    minter="camoufox",
    impersonate="firefox147",
    firefox_major=147,
    accept_language="ru-RU,ru;q=0.9",
    sends_sec_ch_ua=False,
)

PROFILES: dict[str, FingerprintProfile] = {"ozon": FIREFOX, "wb": FIREFOX, "ym": FIREFOX}


def firefox_major(user_agent: str) -> int | None:
    m = _FF_MAJOR.search(user_agent)
    return int(m.group(1)) if m else None


def pick_target(browser_major: int) -> int:
    """Самая новая доступная цель, не новее браузера.

    Подделывать версию новее установленной нельзя: TLS-отпечаток из будущего
    сам по себе аномалия.
    """
    usable = [t for t in AVAILABLE_FIREFOX_TARGETS if t <= browser_major]
    if not usable:
        raise FingerprintDrift(
            f"curl_cffi has no Firefox target at or below {browser_major}; "
            f"available: {AVAILABLE_FIREFOX_TARGETS}"
        )
    return max(usable)


def check_mint(user_agent: str, profile: FingerprintProfile) -> None:
    """Сверка при минтинге: совместимость, а не равенство.

    Равенства между Camoufox и curl_cffi не бывает — у них независимые циклы
    релизов (замер: Firefox 152 против цели 147). Поэтому проверяются два
    условия, каждое из которых можно нарушить по-настоящему:

    * подделываемый мажор не новее браузерного;
    * разрыв в пределах :data:`MAX_MAJOR_GAP`.
    """
    major = firefox_major(user_agent)
    if major is None:
        raise FingerprintDrift(f"not a Firefox user agent: {user_agent!r}")
    if profile.firefox_major > major:
        raise FingerprintDrift(
            f"profile impersonates Firefox/{profile.firefox_major} but the browser is "
            f"Firefox/{major}: forging a version that does not exist yet is itself a tell"
        )
    gap = major - profile.firefox_major
    if gap > MAX_MAJOR_GAP:
        raise FingerprintDrift(
            f"camoufox reports Firefox/{major}, curl_cffi impersonates "
            f"{profile.firefox_major}: gap {gap} exceeds MAX_MAJOR_GAP={MAX_MAJOR_GAP}. "
            f"Bump curl_cffi (available targets: {AVAILABLE_FIREFOX_TARGETS}) or pin "
            f"an older camoufox."
        )


def replay_headers(profile: FingerprintProfile, observed_ua: str) -> dict[str, str]:
    """Заголовки реплея.

    UA берётся НАБЛЮДЁННЫЙ при минтинге, а не константа из профиля: реплей
    замороженного UA рядом с cookie, снятыми браузером с другим UA, разрушает
    ровно ту согласованность, ради которой всё это и делается.
    """
    headers = {
        "User-Agent": observed_ua,
        "Accept-Language": profile.accept_language,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if profile.sends_sec_ch_ua:  # pragma: no cover - у нас Firefox
        headers["sec-ch-ua"] = f'"Chromium";v="{profile.firefox_major}"'
    return headers
