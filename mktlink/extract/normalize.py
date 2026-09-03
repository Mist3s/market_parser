"""Нормализация текста и денег. СКОПИРОВАНО из ``market_parser/normalize.py``.

Копия, а не импорт, и это осознанно: батч-скрейпер живёт своей жизнью, и
привязка сервиса к его релизному циклу означала бы, что правка в парсере
детского питания может изменить ответ этого API. Взято только то, что
действительно чистое и универсальное.

Что НЕ перенесено и почему:

* ``KNOWN_BRANDS`` и ``guess_brand`` — 93 бренда детского питания плюс
  эвристика «первый токен с заглавной буквы». На произвольной пользовательской
  ссылке это генератор неправды: бренд либо есть отдельным полем, либо
  ``null``. Прямой запрет «продавец не угадывается» распространяется и на
  бренд как источник продавца.
* ``choose_price_fields`` — эвристика «max = regular, min = promo» по списку
  чисел, выдранных из ТЕКСТА. У наших маркетплейсов цены приходят именованными
  полями, угадывать нечего, а применение к карточке внесло бы шум от цен
  рекомендаций.
* ``product_key`` — форма верная, но не различает пространства идентификаторов;
  свою версию с префиксом пространства держит :mod:`mktlink.urls.canonical`.

Главное, ради чего копия и нужна: ``parse_price_to_kopecks`` считает через
``Decimal`` с ``ROUND_HALF_UP``. Репозиторный ``_yandex_price_value`` делает
``int(float(v) * 100)`` и теряет копейку на целом классе значений — проверено
на этом чекауте: ``"19.99"`` даёт 1998 вместо 1999, ``"1.15"`` — 114 вместо
115, ``"0.29"`` — 28 вместо 29.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SPACE_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u2007": " ",
        "\u2009": " ",
        "\u202f": " ",
        "\u2006": " ",
        "\u2060": "",
    }
)

PRICE_RE = re.compile(r"\d[\d\s.,]*")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).translate(SPACE_TRANSLATION)
    return re.sub(r"\s+", " ", text).strip()


def parse_price_to_kopecks(value: str | int | float | Decimal | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return _decimal_to_kopecks(Decimal(str(value)))
    if isinstance(value, Decimal):
        return _decimal_to_kopecks(value)

    text = normalize_text(str(value))
    if not text:
        return None
    matches = extract_price_values(text)
    return matches[0] if matches else None


def wb_units_to_kopecks(value: int | None) -> int | None:
    """Wildberries API returns prices in kopecks-like integer units."""
    if value is None:
        return None
    return int(value)


def extract_price_values(text: str) -> list[int]:
    normalized = normalize_text(text)
    values: list[int] = []
    for match in PRICE_RE.findall(normalized):
        token = match.strip(" .,")
        if not token:
            continue
        parsed = _parse_price_token(token)
        if parsed is not None and parsed > 0:
            values.append(parsed)
    return values


def _parse_price_token(token: str) -> int | None:
    token = token.replace(" ", "")
    if not token:
        return None

    decimal_sep = None
    if "," in token and "." in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
    elif "," in token:
        decimal_sep = "," if len(token.rsplit(",", 1)[1]) in {1, 2} else None
    elif "." in token:
        decimal_sep = "." if len(token.rsplit(".", 1)[1]) in {1, 2} else None

    if decimal_sep:
        whole, fraction = token.rsplit(decimal_sep, 1)
        whole = re.sub(r"\D", "", whole)
        fraction = re.sub(r"\D", "", fraction)[:2].ljust(2, "0")
        number = f"{whole}.{fraction}"
    else:
        number = re.sub(r"\D", "", token)

    if not number:
        return None
    try:
        return _decimal_to_kopecks(Decimal(number))
    except InvalidOperation:
        return None


def _decimal_to_kopecks(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kopecks_to_rubles(value: int | None) -> float | None:
    if value is None:
        return None
    return value / 100
