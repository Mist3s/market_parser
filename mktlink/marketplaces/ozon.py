"""Ozon: composer-api и разбор карточки.

Форма URL взята из репозитория дословно (``ozon.py:22``): composer-api
принимает путь страницы в query-параметре ``url``, закодированный целиком.

**Чего из репозитория брать НЕЛЬЗЯ.** ``parse_ozon_widgets`` (``ozon.py:105``)
фильтрует ключи ``widgetStates`` по подстрокам ``tile`` и ``searchresult``.
Это КАТЕГОРИЙНЫЕ ключи — они существуют на листинге, где виджет рисует плитки.
На карточке товара их нет, и функция вернула бы пустой список.

**Что брать нужно.** Технику: состояния виджетов приходят JSON-СТРОКАМИ внутри
JSON (``ozon.py:258``), а поля внутри находятся по собственным QA-атрибутам
Ozon (``automatizationId``), потому что от них зависит его же тест-сьют, и они
переживают редизайн лучше, чем классы вёрстки.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Final
from urllib.parse import quote

from mktlink.extract.htmlmeta import meta_content
from mktlink.extract.htmlmeta import title as html_title
from mktlink.extract.jsonscan import loads_maybe, walk
from mktlink.extract.normalize import normalize_text
from mktlink.extract.seller import classify, make_source
from mktlink.marketplaces.base import RungResult
from mktlink.marketplaces.selectors import SelectorMap
from mktlink.marketplaces.verdict import SellerStatus, Verdict

ORIGIN = "https://www.ozon.ru"
#: Форма из репозитория, ozon.py:22. Путь кодируется целиком, safe="".
COMPOSER_API = ORIGIN + "/api/composer-api.bx/page/json/v2?url="

#: Минимум байт осмысленного ответа composer. Меньше — это заглушка.
MIN_PAYLOAD_BYTES = 2048

#: Подписи страниц блока, ЗАМЕРЕННЫЕ 2026-09-03 настоящим Camoufox 152.
#: Нужны для диагностики, а НЕ для решения: решает HTTP-статус, потому что
#: первая из этих страниц вообще не содержит слов про робота или капчу.
#:
#: Два разных ответа на один и тот же URL, и разница несёт смысл:
#:
#: * ``«Похоже, нет соединения»`` (5.1 КБ) — через датацентровый IPv4 proxy6.
#:   Замаскированный отказ: челлендж НЕ предложен вовсе, адрес в блок-листе.
#: * ``«Antibot Captcha»`` (2.4 КБ) — напрямую, без прокси. Челлендж
#:   предложен, то есть адрес всего лишь не доверенный, а не запрещённый.
#:
#: Прокси даёт ХУДШИЙ ответ, чем его отсутствие. Это и есть измеренная цена
#: датацентрового адреса у Ozon.
BLOCK_SIGNATURES: tuple[str, ...] = (
    "похоже, нет соединения",
    "antibot captcha",
)


#: Минимум байт настоящей отрендеренной карточки. Замер 2026-09-03: живая
#: страница — 730 927 байт, страница блока — 5 221, страница челленджа — 2 369.
#: Порог поставлен на порядок выше обеих отбраковываемых величин и на порядок
#: ниже настоящей, поэтому его точность не важна.
MIN_HTML_BYTES: Final[int] = 20_000

_H1: Final[re.Pattern[str]] = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")

#: Продавец на отрендеренной карточке. ЗАМЕР 2026-09-03: ссылка на магазин
#: несёт имя ТРИЖДЫ и согласованно — в атрибуте ``title``, в тексте ссылки и в
#: ``alt`` соседней картинки, а слаг лежит в ``href``:
#:
#: ``<a title="Шуняня" href="https://www.ozon.ru/seller/shunana/" …>Шуняня</a>``
#:
#: Требовать оба якоря сразу — имя И слаг — это и есть отказ от догадки:
#: совпадение по одному атрибуту было бы случайным, по двум — нет. Слаг вдобавок
#: даёт идентификатор продавца, которого в тексте страницы больше нигде нет.
_SELLER_LINK: Final[re.Pattern[str]] = re.compile(
    r'<a\s+title="([^"]{1,80})"\s+href="https://www\.ozon\.ru/seller/([a-z0-9][a-z0-9\-]{0,60})/"',
    re.I,
)

#: Хвост, который Ozon приклеивает к ``<title>``. Отрезается, потому что в
#: ответе нужно название товара, а не заголовок страницы. ``og:title`` и ``h1``
#: чистые, поэтому этот хвост — крайний случай, когда их обоих нет.
_TITLE_TAIL: Final[re.Pattern[str]] = re.compile(r"\s+купить на OZON.*$", re.S)


def composer_url(path: str) -> str:
    return COMPOSER_API + quote(path, safe="")


def pdp_path(sku: str, slug: str | None = None) -> str:
    return f"/product/{slug}-{sku}/" if slug else f"/product/{sku}/"


def widget_states(payload: Any) -> dict[str, Any]:
    """Развернуть ``widgetStates``: значения — строки с JSON."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("widgetStates")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        parsed = loads_maybe(value)
        if parsed is not None:
            out[key] = parsed
    return out


def _by_qa_id(state: Any, wanted: str) -> str | None:
    """Найти текст по собственному QA-атрибуту Ozon.

    Техника из репозитория: ``_tile_title`` (``ozon.py:181``) ключуется на
    ``textDS.testInfo.automatizationId``, а не на структуре вёрстки.
    """
    for _, node in walk(state):
        if not isinstance(node, dict):
            continue
        text_ds = node.get("textDS")
        if isinstance(text_ds, dict):
            info = text_ds.get("testInfo") or {}
            if info.get("automatizationId") == wanted:
                text = text_ds.get("text")
                if text:
                    return normalize_text(str(text))
        info = node.get("testInfo") or {}
        if isinstance(info, dict) and info.get("automatizationId") == wanted:
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                return normalize_text(text)
    return None


def parse_pdp(payload: Any, sel: SelectorMap, *, sku: str) -> RungResult:
    """Разобрать ответ composer для карточки.

    Классификация вердикта важнее самого разбора: молчаливый ноль и дрейф
    разметки выглядят одинаково для вызывающего, но означают разное — первый
    списывает адрес, второй нет.
    """
    states = widget_states(payload)
    if not states:
        return RungResult(verdict=Verdict.SILENT_EMPTY)

    name = None
    for key, state in states.items():
        if any(w.lower() in key.lower() for w in sel.name_widgets):
            name = _by_qa_id(state, "webProductHeading") or _first_title(state)
            if name:
                break
    if name is None:
        # Ключи есть, а нашего среди них нет: это дрейф разметки, а не блок.
        # Разница несёт деньги — за дрейф адрес не списывается.
        name = _by_qa_id(payload, "webProductHeading")

    seller = _find_seller(states, sel, sku=sku)

    if name is None and seller.name is None:
        return RungResult(verdict=Verdict.SCHEMA_DRIFT, raw=payload)
    return RungResult(
        verdict=Verdict.OK if (name and seller.name) else Verdict.PARTIAL,
        name=name,
        seller_name=seller.name,
        seller_id=seller.seller_id,
        seller_status=seller.status,
        seller_source=seller.source,
        legal_name=seller.legal_name,
        raw=payload,
    )


def _first_title(state: Any) -> str | None:
    for _, node in walk(state):
        if isinstance(node, dict):
            t = node.get("title") or node.get("name")
            if isinstance(t, str) and t.strip():
                return normalize_text(t)
    return None


def _find_seller(states: dict[str, Any], sel: SelectorMap, *, sku: str):
    for key, state in states.items():
        if not any(w.lower() in key.lower() for w in sel.seller_widgets):
            continue
        for path in sel.seller_paths:
            value = _dig(state, path)
            if isinstance(value, str) and value.strip():
                return classify(
                    "ozon",
                    value,
                    make_source("ozon", "state", (key, *path)),
                    _neighbours(state),
                )
    return classify("ozon", None, None, {})


def _dig(node: Any, path: tuple[str, ...]) -> Any:
    cur = node
    for seg in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _neighbours(state: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(state, dict):
        for k in ("id", "sellerId", "shopId"):
            if k in state:
                out["seller_id"] = state[k]
                break
    return out


def classify_response(body: bytes | str, payload: Any, status: int = 200) -> Verdict | None:
    """Вердикт по форме ответа, до разбора содержимого.

    **Статус решает раньше тела, и это исправление по живому замеру.**
    Прежняя версия смотрела только на тело и на реальном блоке Ozon
    (HTTP 403, 5 КБ стилизованной страницы) выдавала ``SILENT_EMPTY``.
    Для здоровья прокси исход тот же — оба вердикта штрафуют, — но диагноз
    оператору выдавался неверный: «страница пришла пустая» вместо «нас
    заблокировали». Замерено 2026-09-03 через датацентровый IPv4 proxy6.

    Отдельная тонкость: страница блока Ozon НЕ содержит ни одного
    антибот-маркера из унаследованного списка — её заголовок «Похоже, нет
    соединения», и слова «робот» или «капча» в ней отсутствуют. Опираться на
    текст тела здесь нельзя вовсе, только на статус; подписи из
    :data:`BLOCK_SIGNATURES` служат диагностике, а не решению.

    Возвращает ``None``, когда ответ похож на нормальный и разбирать его надо.
    """
    if status in (401, 403):
        return Verdict.CAPTCHA
    if status == 429:
        return Verdict.HTTP_429
    if status >= 500:
        return Verdict.UPSTREAM_ERROR
    if status in (301, 302, 303, 307, 308):
        # Рукопожатие за cookie: Ozon отдаёт ``__Secure-ETC`` и отправляет на
        # тот же URL. Само по себе это не отказ, но данных в таком ответе нет.
        return Verdict.SILENT_EMPTY
    size = len(body) if isinstance(body, (bytes, str)) else 0
    if size < MIN_PAYLOAD_BYTES:
        return Verdict.SILENT_EMPTY
    if not isinstance(payload, dict) or "widgetStates" not in payload:
        return Verdict.SILENT_EMPTY
    return None


def identity_html(html: str) -> str | None:
    """Чей это URL по мнению самой страницы.

    ЗАМЕР 2026-09-03: отрендеренная карточка Ozon несёт
    ``<meta property="og:url">`` с полным адресом, включая артикул. Ни
    страница блока (5 КБ), ни страница челленджа (2.4 КБ) его не содержат,
    поэтому предикат разделяет «наша карточка» и «нам подсунули другое».

    Читается через :mod:`mktlink.extract.htmlmeta`, а не собственной
    регуляркой: порядок атрибутов в чужом HTML не гарантирован, и
    предположение о нём один раз уже дало ложный диагноз по целому
    маркетплейсу — см. докстроку того модуля.
    """
    return meta_content(html, "og:url")


def classify_html(html: str, *, anchor_ids: set[str], status: int = 200) -> Verdict | None:
    """Вердикт по отрендеренному HTML. ``None`` — разбирать дальше.

    Отдельная функция, а не ветка в :func:`classify_response`, потому что у
    двух транспортов разные признаки отказа и смешивать их значит получить
    предикат, который нельзя проверить ни на одном настоящем ответе.
    """
    if status in (401, 403):
        return Verdict.CAPTCHA
    if status == 429:
        return Verdict.HTTP_429
    if status >= 500:
        return Verdict.UPSTREAM_ERROR

    low = html.casefold()
    # Подписи — диагностика, но здесь они И решают: страница блока приходит
    # со статусом 200, когда её отдаёт скрейпинг-API, а не Ozon напрямую.
    if any(sig in low for sig in BLOCK_SIGNATURES):
        return Verdict.CAPTCHA
    # Интерстишл JS-рукопожатия: замерено 2026-09-03 с российского
    # резидентного адреса без рендеринга — 13 КБ, заголовок «Происходит
    # перенаправление». Это НЕ отказ и НЕ блок: адрес принят, но данных в
    # ответе нет, потому что не выполнен JS. Штрафовать за это адрес нельзя.
    if "происходит перенаправление" in low:
        return Verdict.CLIENT_RENDERED
    if len(html) < MIN_HTML_BYTES:
        return Verdict.SILENT_EMPTY

    ident = identity_html(html)
    if ident is None:
        # Тело большое, а идентичности нет: разметка поехала. Адрес отработал,
        # поэтому это дрейф, а не улика против прокси.
        return Verdict.SCHEMA_DRIFT
    if anchor_ids and not any(a in ident for a in anchor_ids):
        return Verdict.SCHEMA_DRIFT
    return None


def parse_pdp_html(html: str, *, anchor_ids: set[str], status: int = 200) -> RungResult:
    """Разобрать отрендеренную карточку.

    Появилось по замеру 2026-09-03: через скрейпинг-API с российского
    резидентного адреса и включённым рендерингом Ozon отдаёт полную страницу
    (730 927 байт), в которой ``widgetStates`` НЕТ НИ ОДНОГО — состояние
    виджетов в ней разложено по ``data-widget`` и инлайн-JSON другой формы.
    Поэтому :func:`parse_pdp`, написанный под composer-api, на этой странице
    возвращал ``silent_empty``, то есть сообщал о блоке там, где данные есть.
    """
    verdict = classify_html(html, anchor_ids=anchor_ids, status=status)
    if verdict is not None:
        return RungResult(verdict=verdict)

    name = _html_name(html)
    seller = _SELLER_LINK.search(html)

    if name is None and seller is None:
        return RungResult(verdict=Verdict.SCHEMA_DRIFT)
    if seller is None:
        # Название есть, продавца нет. Не выдумываем: у карточки Ozon продавцом
        # может быть сам маркетплейс, и тогда блока магазина не будет вовсе —
        # но отличить это от дрейфа по одной странице нельзя, а врать нельзя.
        return RungResult(verdict=Verdict.PARTIAL, name=name)

    seller_name = normalize_text(_html.unescape(seller.group(1)))
    slug = seller.group(2)
    return RungResult(
        verdict=Verdict.OK if name else Verdict.PARTIAL,
        name=name,
        seller_name=seller_name or None,
        seller_id=slug,
        seller_status=SellerStatus.RESOLVED if seller_name else SellerStatus.UNKNOWN_LAYOUT,
        seller_source="ozon:html:a[title][href^=/seller/]",
    )


def _html_name(html: str) -> str | None:
    """Название товара. Порядок источников — от чистого к грязному.

    ``h1`` и ``og:title`` на замеренной странице совпадают дословно и уже
    чистые; ``<title>`` несёт хвост «купить на OZON по низкой цене (артикул)»,
    поэтому используется последним и с отрезанным хвостом.
    """
    m = _H1.search(html)
    if m:
        text = normalize_text(_html.unescape(_TAG.sub(" ", m.group(1))))
        if text:
            return text
    og = meta_content(html, "og:title")
    if og:
        text = normalize_text(og)
        if text:
            return text
    raw = html_title(html)
    if raw:
        text = normalize_text(_TITLE_TAIL.sub("", raw))
        if text:
            return text
    return None


__all__ = [
    "COMPOSER_API",
    "MIN_HTML_BYTES",
    "MIN_PAYLOAD_BYTES",
    "ORIGIN",
    "SellerStatus",
    "classify_html",
    "classify_response",
    "composer_url",
    "identity_html",
    "parse_pdp",
    "parse_pdp_html",
    "pdp_path",
    "widget_states",
]
