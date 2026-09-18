"""Название товара из HTML обычного магазина. Один экстрактор на все магазины.

ЗАМЕР 2026-09-18 по пятнадцати карточкам с четырнадцати сайтов (Bitrix, Tilda,
OpenCart, WooCommerce, Shop-Script, Inertia, Nuxt, Vue-SPA): у **каждой** есть
ровно один содержательный ``<h1>`` с названием товара — после того как SPA
отдана в отрендеренном виде (см. ``prerender`` в реестре). Остальные источники
хуже и служат страховкой, а не основой:

* JSON-LD ``Product.name`` есть у 5 из 15 и иногда с мусором — у aromatchaya
  в него вписан артикул («13156 “Гун Тин Гун Бин” …»).
* ``og:title`` есть у 12 из 15, но чаще всего это SEO-строка с «купить в
  Москве» и ценой.
* microdata ``itemprop=name`` внутри ``schema.org/Product`` у kofcheg отдаёт
  «Главная» — первым ``name`` в области оказывается хлебная крошка, — а у
  chaekshop имя СОСЕДНЕГО товара из блока рекомендаций.
* ``<title>`` — всегда SEO-строка; чистится регулярками, и это последний
  рубеж, а не источник.

Поэтому порядок по умолчанию: ``h1``, ``ldjson``, ``og:title``, ``microdata``,
``title``. Магазин, у которого общий порядок даёт мусор, переопределяет его
одним полем в реестре, а не новым парсером.

Разбор регулярками, а не DOM: страницы весят до 900 KiB (Tilda), а нужен один
тег. Та же дисциплина, что в :mod:`mktlink.extract.htmlmeta`.
"""

from __future__ import annotations

import html as _html
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

from mktlink.extract.htmlmeta import meta_content
from mktlink.extract.htmlmeta import title as html_title
from mktlink.extract.normalize import normalize_text

#: Порядок источников по умолчанию. Обоснование — в докстроке модуля.
DEFAULT_NAME_SOURCES: Final[tuple[str, ...]] = ("h1", "ldjson", "og:title", "microdata", "title")

_H1: Final[re.Pattern[str]] = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.S | re.I)
_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_LD: Final[re.Pattern[str]] = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script\s*>",
    re.S | re.I,
)
_MICRO_PRODUCT: Final[re.Pattern[str]] = re.compile(
    r"itemtype\s*=\s*[\"']https?://schema\.org/Product[\"']", re.I
)
_ITEMPROP_NAME: Final[re.Pattern[str]] = re.compile(
    r"<(\w+)\b([^>]*\bitemprop\s*=\s*[\"']name[\"'][^>]*)>", re.I
)
_CONTENT: Final[re.Pattern[str]] = re.compile(r"\bcontent\s*=\s*[\"']([^\"']*)[\"']", re.I)
#: Сколько символов после ``itemtype=Product`` считаем областью товара.
_MICRO_SCOPE: Final[int] = 20_000

#: Тексты, которые бывают в ``<h1>`` у страниц, НЕ являющихся карточкой.
STOPLIST: Final[frozenset[str]] = frozenset(
    s.casefold()
    for s in (
        "Главная",
        "Каталог",
        "Корзина",
        "Поиск",
        "Результаты поиска",
        "Страница не найдена",
        "Товар не найден",
        "Ошибка 404",
        "404",
        "Page not found",
        "Not found",
    )
)

#: Признаки «мягкого 404»: сервер ответил 200, а страница говорит «нет такого».
NOT_FOUND_MARKERS: Final[re.Pattern[str]] = re.compile(
    r"(страница не найдена|товар не найден|page not found|ошибка 404|error 404|"
    r"такой страницы (не существует|нет))",
    re.I,
)

#: Сильные разделители SEO-заголовка: первый сегмент — название, дальше сайт.
_STRONG_SEP: Final[re.Pattern[str]] = re.compile(r"\s+(?:\||—|–|::|»)\s+")
#: Слабые разделители: режем только если за ними идёт маркетинговый хвост.
_WEAK_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:-|:|,)\s+(?=(?:купить|заказать|цена|цены|недорого|дешево|оптом|"
    r"интернет|в интернет-магазине|доставка|в наличии|с доставкой|по (?:выгодной|низкой) цене))",
    re.I,
)
_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:купить|заказать)(?:\s.*)?$|\s+(?:в|по)\s+интернет-магазине\b.*$", re.I
)
_LEAD: Final[re.Pattern[str]] = re.compile(
    r"^(?:[^:]{1,40}:\s*)?(?:купить|заказать)\s+", re.I
)
_PRICE_TAIL: Final[re.Pattern[str]] = re.compile(r"\s+[—–-]\s+[\d\s]+₽.*$")


@dataclass(frozen=True, slots=True)
class Extracted:
    """Результат: название, откуда взято и все кандидаты для диагностики.

    Кандидаты уходят в ``meta.detail`` при отказе: когда разметка уехала,
    человеку нужно видеть, что именно нашлось, а не только «ничего».
    """

    name: str | None
    source: str | None
    candidates: tuple[tuple[str, str], ...]


def extract_name(html: str, sources: tuple[str, ...] = DEFAULT_NAME_SOURCES) -> Extracted:
    """Первый пригодный кандидат в порядке ``sources``."""
    candidates: list[tuple[str, str]] = []
    for src in sources:
        fn = _SOURCES.get(src)
        if fn is None:
            raise ValueError(f"unknown name source {src!r}")
        value = fn(html)
        if value:
            candidates.append((src, value))
    for src, value in candidates:
        if acceptable(value):
            return Extracted(name=value, source=src, candidates=tuple(candidates))
    return Extracted(name=None, source=None, candidates=tuple(candidates))


def acceptable(name: str) -> bool:
    """Похоже ли на название товара, а не на служебный заголовок."""
    text = name.strip()
    if len(text) < 3 or len(text) > 300:
        return False
    if text.casefold() in STOPLIST:
        return False
    return not text.replace(" ", "").isdigit()


def site_name(html: str) -> str | None:
    """``og:site_name`` — единственный формальный источник имени магазина."""
    value = meta_content(html, "og:site_name")
    return normalize_text(value) or None if value else None


def looks_like_not_found(html: str) -> bool:
    """Мягкий 404: ``<title>`` или ``<h1>`` говорят, что страницы нет."""
    parts = [html_title(html) or "", *(_text(h) for h in _H1.findall(html))]
    return any(NOT_FOUND_MARKERS.search(p) for p in parts if p)


def clean_title(text: str | None) -> str | None:
    """Снять с SEO-заголовка «купить в Москве», цену и имя сайта.

    Правила выведены из пятнадцати живых заголовков, и каждое ловит
    конкретный случай:

    * ``Улун Габа … — 2 280 ₽, купить | Art of Tea`` — сильный разделитель,
      берём первый сегмент;
    * ``Чай Габа Улун - купить в Москве по выгодной цене`` — слабый ``-``,
      режем только потому, что дальше «купить»; в ``Белый чай - Бай Хао Инь
      Чжэнь`` тот же дефис остаётся на месте;
    * ``Китайский чай: купить Белый чай - Бай Хао …`` — категория и «купить»
      спереди;
    * ``Да Хун Пао Нун Сян купить в Москве`` — хвост без разделителя.
    """
    if not text:
        return None
    value = normalize_text(text)
    value = _STRONG_SEP.split(value, maxsplit=1)[0]
    value = _WEAK_TAIL.split(value, maxsplit=1)[0]
    value = _PRICE_TAIL.sub("", value)
    value = _LEAD.sub("", value)
    value = _TAIL.sub("", value)
    value = value.strip(" ,.-–—|:")
    return value or None


# --- источники ------------------------------------------------------------------


def _from_h1(html: str) -> str | None:
    """Единственный ``<h1>`` — ответ. Несколько — ближайший к ``<title>``.

    Несколько h1 бывает у шаблонов, где логотип или слоган тоже h1; тогда
    выбирается тот, у которого больше общих слов с заголовком страницы, —
    SEO-строка почти всегда содержит название товара.
    """
    texts = [t for t in (_text(h) for h in _H1.findall(html)) if t]
    if not texts:
        return None
    if len(texts) == 1:
        return texts[0]
    ref = _tokens(html_title(html) or "")
    if not ref:
        return texts[0]
    return max(texts, key=lambda t: (len(_tokens(t) & ref), -texts.index(t)))


def _from_ldjson(html: str) -> str | None:
    for obj in _iter_ld(html):
        for node in _walk(obj):
            if not isinstance(node, dict) or not _is_product(node.get("@type")):
                continue
            name = node.get("name")
            if isinstance(name, str) and normalize_text(name):
                return normalize_text(name)
    return None


def _from_og_title(html: str) -> str | None:
    return clean_title(meta_content(html, "og:title"))


def _from_microdata(html: str) -> str | None:
    """``itemprop=name`` в первой области ``schema.org/Product``."""
    m = _MICRO_PRODUCT.search(html)
    if m is None:
        return None
    scope = html[m.end() : m.end() + _MICRO_SCOPE]
    for tag in _ITEMPROP_NAME.finditer(scope):
        attrs = tag.group(2)
        c = _CONTENT.search(attrs)
        if c is not None and c.group(1).strip():
            return normalize_text(_html.unescape(c.group(1)))
        close = re.compile(rf"</{tag.group(1)}\s*>", re.I).search(scope, tag.end())
        if close is not None:
            text = _text(scope[tag.end() : close.start()])
            if text:
                return text
    return None


def _from_title(html: str) -> str | None:
    return clean_title(html_title(html))


_SOURCES: Final[dict[str, Callable[[str], str | None]]] = {
    "h1": _from_h1,
    "ldjson": _from_ldjson,
    "og:title": _from_og_title,
    "microdata": _from_microdata,
    "title": _from_title,
}


# --- вспомогательное ------------------------------------------------------------


def _text(fragment: str) -> str:
    return normalize_text(_html.unescape(_TAG.sub(" ", fragment)))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.casefold()) if len(w) > 2}


def _is_product(t: Any) -> bool:
    if isinstance(t, str):
        return t.rsplit("/", 1)[-1] == "Product"
    if isinstance(t, list):
        return any(_is_product(x) for x in t)
    return False


def _iter_ld(html: str) -> Iterator[Any]:
    for m in _LD.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except ValueError:
            # Невалидный JSON-LD — обычное дело у шаблонов; это не отказ,
            # просто источника нет.
            continue


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


__all__ = [
    "DEFAULT_NAME_SOURCES",
    "Extracted",
    "acceptable",
    "clean_title",
    "extract_name",
    "looks_like_not_found",
    "site_name",
]
