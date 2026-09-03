"""Wildberries: единственный лейн, где продавец почти бесплатен.

Репозиторий уже получает поле ``supplier`` в каталожном объекте и теряет его:
``wildberries.py:300`` кладёт его в ``raw`` и никуда не выводит. Это
наблюдение — самая ценная вещь, которую даёт репозиторий для нашей задачи.

Что здесь гипотеза, а что нет:

* ``supplier`` в объекте товара — **[репо]**, наблюдается в каталожном ответе.
* Эндпоинт карточки ``card.wb.ru/cards/v2/detail`` — **[гипотеза]**: в
  репозитории он не встречается ни разу, там ходят в каталог
  ``catalog.wb.ru/catalog/product5/v4/catalog``.
* ``dest=-1257786`` и ``spp=30`` — **[репо]**, ``_catalog_params``. ``dest``
  задаёт регион и меняет цену с наличием, поэтому дрейфовать ему нельзя.
* ``supplierId`` — **[гипотеза]**: в репозиторных полях отсутствует.

Ещё одно наблюдение, которое стоит перенести: WB отдаёт цены в целых
единицах, кратных копейке, и репозиторный ``wb_units_to_kopecks`` — это
тождественная функция. Умножать их на сто, как обычные рубли, нельзя.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from mktlink.extract.normalize import normalize_text, wb_units_to_kopecks
from mktlink.extract.seller import classify, make_source
from mktlink.marketplaces.base import RungResult
from mktlink.marketplaces.selectors import SelectorMap
from mktlink.marketplaces.verdict import SellerStatus, Verdict

CARD_API = "https://card.wb.ru/cards/v2/detail"
PDP = "https://www.wildberries.ru/catalog/{nm}/detail.aspx"

#: [репо] wildberries.py:262. Регион Москвы. Меняет цену и наличие, поэтому
#: значение фиксированное, а не «какое получится».
DEST = "-1257786"
SPP = "30"


def card_url(nm: str) -> str:
    return f"{CARD_API}?" + urlencode(
        {"appType": "1", "curr": "rub", "dest": DEST, "spp": SPP, "nm": nm}
    )


def pdp_url(nm: str) -> str:
    """[репо] Форма из wildberries.py:293."""
    return PDP.format(nm=nm)


def parse_card(payload: Any, sel: SelectorMap, *, nm: str) -> RungResult:
    """Разобрать ответ карточки.

    Продавец здесь берётся полем, а не поиском по дереву: у WB одна карточка —
    один продавец, агрегации офферов нет, поэтому и якорить нечего сверх
    совпадения ``id``.
    """
    products = _products(payload)
    if not products:
        return RungResult(verdict=Verdict.SILENT_EMPTY, raw=payload)

    item = _pick(products, nm)
    if item is None:
        # Ответ есть, товары есть, а нашего среди них нет. Это не блок: адрес
        # отработал. Либо товар снят, либо мы ошиблись идентификатором.
        return RungResult(verdict=Verdict.NOT_FOUND, raw=payload)

    name = normalize_text(str(item.get("name") or "")) or None

    seller_value = None
    used_path: tuple[str, ...] = ()
    for path in sel.seller_paths or (("supplier",),):
        value = item.get(path[0]) if len(path) == 1 else None
        if isinstance(value, str) and value.strip():
            seller_value = value
            used_path = path
            break

    seller = classify(
        "wb",
        seller_value,
        make_source("wb", "state", ("data", "products", "0", *used_path))
        if seller_value
        else None,
        {"supplierId": item.get("supplierId")},
    )

    if name is None and seller.name is None:
        return RungResult(verdict=Verdict.SCHEMA_DRIFT, raw=payload)
    return RungResult(
        verdict=Verdict.OK if (name and seller.name) else Verdict.PARTIAL,
        name=name,
        seller_name=seller.name,
        seller_id=seller.seller_id,
        seller_status=seller.status,
        seller_source=seller.source,
        raw=payload,
    )


def price_kopecks(item: dict[str, Any]) -> int | None:
    """Цена из объекта товара.

    WB отдаёт целые единицы, кратные копейке: ``wb_units_to_kopecks`` —
    тождество, и умножать их на сто нельзя.
    """
    sizes = item.get("sizes") or []
    if not sizes:
        return None
    price = (sizes[0] or {}).get("price") or {}
    return wb_units_to_kopecks(price.get("product") or price.get("basic"))


def _products(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("products")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _pick(products: list[dict[str, Any]], nm: str) -> dict[str, Any] | None:
    for item in products:
        if str(item.get("id")) == str(nm):
            return item
    return None


def classify_response(payload: Any) -> Verdict | None:
    if not isinstance(payload, dict) or "data" not in payload:
        return Verdict.SILENT_EMPTY
    return None


__all__ = [
    "CARD_API",
    "DEST",
    "SPP",
    "SellerStatus",
    "card_url",
    "classify_response",
    "parse_card",
    "pdp_url",
    "price_kopecks",
]
