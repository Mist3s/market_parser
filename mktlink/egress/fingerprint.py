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
бы вовсе. Инвариантен только МАЖОР Firefox — его и пиним.
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


#: Один профиль на все три маркетплейса: минтит везде Camoufox.
FIREFOX = FingerprintProfile(
    name="ff",
    minter="camoufox",
    impersonate="firefox133",
    firefox_major=133,
    accept_language="ru-RU,ru;q=0.9",
    sends_sec_ch_ua=False,
)

PROFILES: dict[str, FingerprintProfile] = {"ozon": FIREFOX, "wb": FIREFOX, "ym": FIREFOX}


def firefox_major(user_agent: str) -> int | None:
    m = _FF_MAJOR.search(user_agent)
    return int(m.group(1)) if m else None


def check_mint(user_agent: str, profile: FingerprintProfile) -> None:
    """Сверка при минтинге. Не тавтология: связывает сборку браузера с профилем.

    Проверяется мажор, который реально отдала установленная сборка Camoufox,
    против числа в ``impersonate``. Апгрейд Camoufox после этого ломает сборку,
    а не проценты успеха.
    """
    major = firefox_major(user_agent)
    if major is None:
        raise FingerprintDrift(f"not a Firefox user agent: {user_agent!r}")
    if major != profile.firefox_major:
        raise FingerprintDrift(
            f"camoufox reports Firefox/{major}, profile pins {profile.firefox_major}; "
            f"bump {profile.impersonate} together with the browser"
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
