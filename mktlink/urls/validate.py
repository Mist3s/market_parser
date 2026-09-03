"""Гигиена URL до любого сетевого действия.

Всё здесь — чистый CPU, ни одного открытого сокета. Порядок важен: сначала
дешёвые синтаксические отказы, потом IDNA, и только потом сравнение с
allowlist'ом. Сравнивать до нормализации нельзя — иначе гомоглиф ``оzon.ru``
с кириллической «о» пройдёт как незнакомый хост вместо того, чтобы быть
отвергнутым как подделка.
"""

from __future__ import annotations

import ipaddress
import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

#: Максимум байт в поданном URL. Больше — отказ до разбора.
URL_MAX_BYTES: Final[int] = 2048

#: Скрипты, смешение которых в одном хосте есть признак подделки. Кириллица
#: и латиница в одной метке домена легитимно не встречаются.
_CYRILLIC: Final[str] = "CYRILLIC"
_LATIN: Final[str] = "LATIN"


class UrlRejected(ValueError):
    """URL отвергнут на входе. ``code`` уходит в тело ответа как есть."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    scheme: str
    host: str
    path: str
    query: str


def _script_of(ch: str) -> str | None:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    return name.split()[0] if name else None


def _has_mixed_script(label: str) -> bool:
    """Смешение кириллицы и латиницы в одной метке.

    ``uts46=True`` в IDNA этого НЕ ловит, вопреки распространённому мнению:
    он нормализует регистр и запрещённые символы, но ``оzon`` (кириллическая
    «о» плюс латиница) для него валидная метка. Проверка нужна отдельная.
    """
    scripts = {s for s in (_script_of(c) for c in label if c.isalpha()) if s}
    return _CYRILLIC in scripts and _LATIN in scripts


def validate(raw: str) -> ParsedUrl:
    """Разобрать и проверить поданный URL.

    Бросает :class:`UrlRejected` с кодом, который контракт отдаёт клиенту.
    """
    if not raw or not raw.strip():
        raise UrlRejected("invalid_url", "empty")
    if len(raw.encode("utf-8")) > URL_MAX_BYTES:
        raise UrlRejected("invalid_url", f"longer than {URL_MAX_BYTES} bytes")
    if raw != raw.strip():
        raise UrlRejected("invalid_url", "leading or trailing whitespace")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise UrlRejected("invalid_url", "control characters")

    parts = urlsplit(raw)

    if parts.scheme != "https":
        # http:// тоже отказ, а не апгрейд: апгрейд означал бы, что мы
        # согласились сходить туда, куда нас просили по открытому каналу.
        raise UrlRejected("invalid_url", f"scheme must be https, got {parts.scheme!r}")

    # urlsplit не бросает на битом порту — он бросает при обращении к .port.
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejected("invalid_url", "malformed port") from exc
    if port not in (None, 443):
        raise UrlRejected("invalid_url", f"port {port} is not allowed")

    if parts.username is not None or parts.password is not None:
        raise UrlRejected("invalid_url", "userinfo is not allowed")

    host = parts.hostname
    if not host:
        raise UrlRejected("invalid_url", "no host")

    # IP-литерал вместо имени — отказ. Проверяем ДО IDNA, потому что
    # ipaddress отвергает десятичные и восьмеричные формы записи, и такой
    # хост уедет в ветку имени; там его добьёт allowlist, но код ошибки
    # должен быть честным.
    if host.startswith("[") or _is_ip_literal(host):
        raise UrlRejected("invalid_url", "IP literal instead of a hostname")

    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise UrlRejected("invalid_url", "IDNA/UTS-46 failure") from exc

    for label in host.split("."):
        if _has_mixed_script(label):
            raise UrlRejected("invalid_url", f"mixed-script host label: {label!r}")

    path = parts.path or "/"
    return ParsedUrl(scheme="https", host=ascii_host, path=path, query=parts.query)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
