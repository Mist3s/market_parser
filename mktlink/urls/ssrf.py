"""SSRF: allowlist, IP-гигиена и путевое veto на челленджи.

Раскрутка редиректов — это граница безопасности, а не удобство. Отправитель
ссылки контролирует, куда мы пойдём, поэтому проверяется каждый хоп, а не
только первый, и проверяется **каждый** адрес из ответа DNS, а не первый.

Отдельно — дыра, видимая только на Яндексе. Заключение цепочки проверяет
ХОСТ хопа, но Яндекс отдаёт свой челлендж на том же хосте::

    302 Location: https://market.yandex.ru/showcaptcha?cc=1&mt=…&retpath=…

Хост совпадает, правило заключения проходит, и раскрутчик привёл бы нас на
страницу капчи и объявил её каноническим URL. Закрывается путевым veto.

Вердикт при попадании в veto зависит от егресса. Раскрутка идёт **без
прокси**, поэтому челлендж, увиденный в ней, — это ``UNWIND_CHALLENGED``, и
здоровья прокси он не касается вообще: наблюдение, сделанное не через прокси,
не может быть уликой против прокси. Ровно та же дисциплина, что у
``SCHEMA_DRIFT``.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

#: Челлендж-пути на маркетплейсных хостах. Попадание — немедленный терминал
#: и НЕ хоп: продолжать цепочку с капчи некуда.
PATH_VETO: Final[dict[str, re.Pattern[str]]] = {
    "ym": re.compile(r"^/(showcaptcha|checkcaptcha|captcha|login|auth|referer|resolve)\b"),
    "ozon": re.compile(r"^/(challenge|antibot|captcha)\b"),
    "wb": re.compile(r"^/(captcha|challenge)\b"),
}

#: Челлендж, уехавший на другой хост. Литеральный allowlist его и так
#: отвергнет — но отвергнуть надо КАК ЧЕЛЛЕНДЖ, а не как «твоя ссылка плохая».
#: Без этой строки блок маркетплейса рапортуется клиенту ложью, которую он не
#: может опровергнуть.
CHALLENGE_HOST: Final[re.Pattern[str]] = re.compile(r"(^|\.)yandex\.(ru|com|net)$")

_BLOCKED_V4: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),  # CGNAT
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local, сюда же 169.254.169.254
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.0.0.0/24"),
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("198.18.0.0/15"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("224.0.0.0/4"),  # multicast
    ipaddress.IPv4Network("240.0.0.0/4"),
    ipaddress.IPv4Network("255.255.255.255/32"),
)

_BLOCKED_V6: Final[tuple[ipaddress.IPv6Network, ...]] = (
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped: обход через 4-в-6
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64
    ipaddress.IPv6Network("100::/64"),
    ipaddress.IPv6Network("2001:db8::/32"),
    ipaddress.IPv6Network("fc00::/7"),  # unique-local
    ipaddress.IPv6Network("fe80::/10"),  # link-local
    ipaddress.IPv6Network("fec0::/10"),  # site-local: deprecated, но маршрутизируем
    ipaddress.IPv6Network("ff00::/8"),  # multicast
)


class SsrfRejected(ValueError):
    """Адрес или путь отвергнут политикой."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class UnwindChallenged(Exception):
    """Внутри раскрутки нас увели на челлендж.

    Внутри запроса это не лечится, и притворяться иначе не будем: повторить
    хоп через прокси нельзя по бюджету, а перезапрос с того же нашего IP
    повторит челлендж. Непустой счётчик этого события — вход в Phase 0, а не
    рантайм-механизм.
    """

    def __init__(self, url: str, *, egress: str) -> None:
        self.url = url
        self.egress = egress
        super().__init__(f"challenge during redirect unwind at {url!r} (egress={egress})")


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Адрес, на который мы не пойдём ни при каких условиях."""
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in net for net in _BLOCKED_V4):
            return True
    else:
        if any(ip in net for net in _BLOCKED_V6):
            return True
        # IPv4-mapped проверяем ещё и по вложенному адресу: ::ffff:10.0.0.1
        # уже отсечён сетью выше, но маппинг бывает записан и иначе.
        mapped = ip.ipv4_mapped
        if mapped is not None and is_blocked_ip(mapped):
            return True
    # is_global у Python отвечает на тот же вопрос с другой стороны; берём
    # оба, потому что таблицы IANA обновляются, а наши списки нет.
    return not ip.is_global


def check_resolved(addresses: list[str]) -> None:
    """ВСЕ адреса из ответа DNS обязаны пройти. Не первый — все.

    Иначе DNS-ответ с одним публичным и одним приватным адресом проходит
    проверку и подключается к приватному: какой из них выберет ОС, мы не
    контролируем.
    """
    if not addresses:
        raise SsrfRejected("dns_empty", "resolver returned no addresses")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SsrfRejected("dns_malformed", raw) from exc
        if is_blocked_ip(ip):
            raise SsrfRejected("ip_not_global", raw)


def check_path_veto(marketplace: str, path: str, *, egress: str, url: str) -> None:
    """Челлендж на маркетплейсном хосте: терминал, а не хоп."""
    pat = PATH_VETO.get(marketplace)
    if pat is not None and pat.match(path):
        raise UnwindChallenged(url, egress=egress)


def looks_like_challenge_host(host: str) -> bool:
    """Хост челленджа вне allowlist'а — отвергать как челлендж, а не как «плохая ссылка»."""
    return CHALLENGE_HOST.search(host) is not None
