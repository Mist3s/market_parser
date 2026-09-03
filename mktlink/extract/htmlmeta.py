"""Чтение мета-разметки. Порядок атрибутов НЕ предполагается.

Модуль появился из-за дефекта, который стоил неверного диагноза по целому
маркетплейсу, и стоит назвать его прямо.

ЗАМЕР 2026-09-03. Первая редакция читала Open Graph регуляркой вида
``<meta[^>]+property="og:url"[^>]+content="..."`` — то есть требовала, чтобы
``property`` стоял РАНЬШЕ ``content``. Ozon так и отдаёт, поэтому на нём это
работало. Яндекс отдаёт наоборот::

    <meta content="product" property="og:type">
    <meta content="Листовой чай Да Хун Пао …" property="og:title">
    <meta content="https://market.yandex.ru/card/…/101814267477" property="og:url">

Регулярка не совпадала никогда. Последствие было хуже, чем потерянное поле:
``ym.identity()`` возвращал ``None``, из этого ``classify_response`` делал
вывод «ни og:url, ни canonical — это не наша страница» и отдавал
``SILENT_EMPTY``. То есть на полностью корректной карточке с названием и
продавцом система сообщала о молчаливом блоке И ШТРАФОВАЛА АДРЕС: по
:data:`mktlink.marketplaces.verdict.CHARGES_PROXY` ``SILENT_EMPTY`` считается
уликой против прокси. Исправно работающий адрес отправлялся в замену из-за
порядка двух атрибутов в чужом HTML.

Отсюда правило модуля: **имя атрибута ищется независимо от позиции, и это не
придирка к стилю, а условие, без которого предикат нельзя проверить.**

Разбор остаётся регулярками, а не парсером DOM, и это осознанно: на пути
запроса лежит бюджет в единицы секунд, а страница весит 730 КБ. Полный разбор
дерева ради четырёх мета-тегов — плата, которой здесь не за что платить.
"""

from __future__ import annotations

import html as _html
import re
from typing import Final

#: Один тег ``<meta>`` целиком. Дальше атрибуты читаются внутри него, поэтому
#: их взаимный порядок перестаёт иметь значение.
_META_TAG: Final[re.Pattern[str]] = re.compile(r"<meta\s[^>]*>", re.I)
#: ``property`` или ``name``: Open Graph пользуется первым, Twitter Cards —
#: вторым, и оба встречаются на одной странице.
_META_KEY: Final[re.Pattern[str]] = re.compile(
    r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', re.I
)
_META_CONTENT: Final[re.Pattern[str]] = re.compile(
    r'content\s*=\s*["\']([^"\']*)["\']', re.I
)

_LINK_TAG: Final[re.Pattern[str]] = re.compile(r"<link\s[^>]*>", re.I)
_LINK_REL: Final[re.Pattern[str]] = re.compile(r'rel\s*=\s*["\']([^"\']+)["\']', re.I)
_LINK_HREF: Final[re.Pattern[str]] = re.compile(r'href\s*=\s*["\']([^"\']*)["\']', re.I)

_TITLE: Final[re.Pattern[str]] = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def meta_content(html: str, key: str) -> str | None:
    """Содержимое мета-тега по имени. Первое вхождение выигрывает.

    ``key`` сравнивается без учёта регистра: ``og:url`` и ``OG:URL`` — одно и
    то же имя.
    """
    want = key.casefold()
    for tag in _META_TAG.finditer(html):
        raw = tag.group(0)
        k = _META_KEY.search(raw)
        if k is None or k.group(1).casefold() != want:
            continue
        c = _META_CONTENT.search(raw)
        if c is not None and c.group(1):
            return _html.unescape(c.group(1))
    return None


def link_href(html: str, rel: str) -> str | None:
    """``href`` элемента ``<link>`` с заданным ``rel``.

    ``rel`` может нести несколько значений через пробел (``rel="alternate
    canonical"``), поэтому сравнение идёт по токенам, а не по всей строке.
    """
    want = rel.casefold()
    for tag in _LINK_TAG.finditer(html):
        raw = tag.group(0)
        r = _LINK_REL.search(raw)
        if r is None or want not in r.group(1).casefold().split():
            continue
        h = _LINK_HREF.search(raw)
        if h is not None and h.group(1):
            return _html.unescape(h.group(1))
    return None


def title(html: str) -> str | None:
    """Содержимое ``<title>``: теги убраны, сущности развёрнуты, пробелы сжаты.

    Сжатие пробелов здесь, а не у вызывающего: убирая тег, мы САМИ вставляем
    на его место пробел, и оставлять этот мусор в возвращаемом значении
    значило бы требовать уборки от каждого читателя по отдельности.
    """
    m = _TITLE.search(html)
    if m is None:
        return None
    text = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


__all__ = ["link_href", "meta_content", "title"]
