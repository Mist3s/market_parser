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

from typing import Any
from urllib.parse import quote

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
    антибот-маркера из унаследованного списка. Опираться на текст тела здесь
    нельзя вовсе — только на статус.

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


__all__ = [
    "COMPOSER_API",
    "MIN_PAYLOAD_BYTES",
    "ORIGIN",
    "SellerStatus",
    "classify_response",
    "composer_url",
    "parse_pdp",
    "pdp_path",
    "widget_states",
]
