"""Яндекс.Маркет: единственный лейн, где выбор оффера — это выбор продавца.

Одна карточка агрегирует офферы МНОГИХ продавцов, поэтому решение «чей оффер
читаем» и решение «какого продавца вернём» — одно и то же решение. Отсюда
якорный выбор вместо поиска первого подходящего узла.

Регион форсируется на исходящем (``lr=213``), поэтому в ключ кэша он не
входит. Утверждение это держится ровно до Phase 0: если замер покажет, что
Яндекс параметр игнорирует, регион переезжает в ключ и в ответ как
НАБЛЮДЁННЫЙ, а не как утверждённый.

Вердикты у Я.Маркета разделяются дешевле, чем у Ozon, и это стоит знать.
У Ozon блок и дрейф выглядят одинаково — «меньше ключей», — поэтому там
понадобился эмпирический порог по их количеству. Здесь маркеры дизъюнктны:
заблокированная страница не несёт ``og:url`` с нашим идентификатором. Значит
``SCHEMA_DRIFT`` не может быть принят за бан структурно, а не по договорённости.
"""

from __future__ import annotations

import re
from typing import Final

from mktlink.extract.jsonscan import iter_state_candidates
from mktlink.extract.normalize import normalize_text
from mktlink.extract.seller import select_anchored
from mktlink.marketplaces.base import RungResult
from mktlink.marketplaces.selectors import SelectorMap
from mktlink.marketplaces.verdict import SellerStatus, Verdict

ORIGIN = "https://market.yandex.ru"

#: Регион Москвы. Форсируется на КАЖДОМ исходящем.
REGION_ID: Final[int] = 213

#: Минимум байт настоящей карточки. Меньше — заглушка или челлендж.
MIN_PDP_BYTES: Final[int] = 20_000

#: Маркеры челленджа в теле. Список — полевая разведка из репозитория
#: (``html_extractors`` знает про SmartCaptcha), плюс специфика Яндекса.
CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "showcaptcha",
    "checkcaptcha",
    "smartcaptcha",
    "запросы, поступившие с вашего",
    "captcha-image",
)

#: Положительное доказательство отсутствия товара. Не «пусто», а именно «нет».
NOTFOUND_MARKERS: Final[tuple[str, ...]] = (
    "страница не найдена",
    "товар не найден",
    "ничего не найдено",
)

#: Карточка есть, офферов нет. Это ПРАВИЛЬНЫЙ ответ, а не отказ.
NO_OFFERS_MARKERS: Final[tuple[str, ...]] = (
    "нет в продаже",
    "товара нет в наличии",
    "снят с продажи",
)

_OG_URL = re.compile(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)', re.I)
_CANONICAL = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def fetch_url(path: str) -> str:
    """URL исходящего запроса с форсированным регионом."""
    sep = "&" if "?" in path else "?"
    return f"{ORIGIN}{path}{sep}lr={REGION_ID}"


def identity(html: str) -> str | None:
    """Идентичность страницы: чей это URL по её собственному мнению.

    Ключевой предикат классификации. Заблокированная страница ``og:url`` с
    нашим идентификатором не несёт, поэтому его наличие отделяет «нас увели»
    от «разметка поехала».
    """
    for pat in (_OG_URL, _CANONICAL):
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


def classify_response(html: str, *, anchor_ids: set[str], status: int = 200) -> Verdict | None:
    """Вердикт по форме ответа. ``None`` — разбирать дальше.

    Порядок проверок инвертирован против репозиторного ``ensure_not_blocked``:
    там ранний выход по признакам каталога закорачивал сканирование маркеров,
    и настоящая карточка Я.Маркета всегда попадала в этот ранний выход —
    то есть проверка не выполнялась НИКОГДА и её нельзя было провалидировать
    тестом. Сперва маркеры, потом положительная идентичность.
    """
    # Статус решает раньше тела: страница блока может не содержать ни одного
    # текстового маркера (замерено на Ozon, см. его classify_response).
    if status in (401, 403):
        return Verdict.CAPTCHA
    if status == 429:
        return Verdict.HTTP_429
    if status >= 500:
        return Verdict.UPSTREAM_ERROR

    low = html.casefold()

    if any(m in low for m in CAPTCHA_MARKERS):
        return Verdict.CAPTCHA

    ident = identity(html)
    if ident is None:
        # Ни og:url, ни canonical: это не наша страница и не карточка.
        return Verdict.SILENT_EMPTY

    if any(m in low for m in NOTFOUND_MARKERS):
        return Verdict.NOT_FOUND

    if len(html) < MIN_PDP_BYTES:
        return Verdict.SILENT_EMPTY

    if anchor_ids and not any(a in ident for a in anchor_ids):
        # Страница наша, но про другой товар: нас перебросили.
        return Verdict.SCHEMA_DRIFT

    return None


def parse_pdp(
    html: str, sel: SelectorMap, *, anchor_ids: set[str], status: int = 200
) -> RungResult:
    """Разобрать карточку. Продавец берётся ТОЛЬКО якорным выбором."""
    verdict = classify_response(html, anchor_ids=anchor_ids, status=status)
    if verdict is not None:
        return RungResult(verdict=verdict)

    name = _title(html)

    best = None
    for state in iter_state_candidates(html):
        found = select_anchored(state, mp="ym", anchor_ids=anchor_ids)
        if found.status in (SellerStatus.RESOLVED, SellerStatus.FIRST_PARTY):
            best = found
            break
        if best is None:
            best = found

    if best is None:
        # Состояния нет вовсе — но идентичность есть, значит страница наша.
        low = html.casefold()
        status = (
            SellerStatus.NO_OFFERS
            if any(m in low for m in NO_OFFERS_MARKERS)
            else SellerStatus.DEFERRED_RENDER
        )
        return RungResult(
            verdict=Verdict.OK if status is SellerStatus.NO_OFFERS else Verdict.CLIENT_RENDERED,
            name=name,
            seller_status=status,
        )

    complete = bool(name) and best.status in (
        SellerStatus.RESOLVED,
        SellerStatus.FIRST_PARTY,
    )
    return RungResult(
        verdict=Verdict.OK if complete else Verdict.PARTIAL,
        name=name,
        seller_name=best.name,
        seller_id=best.seller_id,
        seller_status=best.status,
        seller_source=best.source,
        legal_name=best.legal_name,
    )


def _title(html: str) -> str | None:
    m = _H1.search(html)
    if not m:
        return None
    text = normalize_text(_TAG.sub(" ", m.group(1)))
    return text or None


__all__ = [
    "CAPTCHA_MARKERS",
    "MIN_PDP_BYTES",
    "NOTFOUND_MARKERS",
    "NO_OFFERS_MARKERS",
    "ORIGIN",
    "REGION_ID",
    "classify_response",
    "fetch_url",
    "identity",
    "parse_pdp",
]
