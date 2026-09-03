"""Пути к полям, вынесенные из кода в конфиг.

Смысл ровно один: **переименование ключа маркетплейсом должно чиниться пушем
конфига, а не деплоем.** Ozon и Я.Маркет меняют имена виджетов без
предупреждения, и разница между «поправили YAML за минуту» и «собрали образ,
прокатили, подождали» — это часы, в течение которых сервис отдаёт null.

Дефолты здесь — ГИПОТЕЗЫ. В репозитории ``market_parser`` нет ни одного
захваченного ответа карточки Ozon или Я.Маркета: он парсит листинги, и
листинговые ключи (``tile``, ``searchresult``) к карточке неприменимы.
Пиннинг делается один раз по живому payload'у скриптом ``scripts/pin_selectors``,
и до этого лейн честно отдаёт ``seller_status = unknown_layout``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SelectorMap:
    """Запиненные пути для одного маркетплейса."""

    marketplace: str
    #: Подстроки ключей ``widgetStates``, в которых лежит заголовок карточки.
    name_widgets: tuple[str, ...] = ()
    #: Подстроки ключей, где лежит блок продавца.
    seller_widgets: tuple[str, ...] = ()
    #: Пути внутри найденного узла, по порядку предпочтения.
    seller_paths: tuple[tuple[str, ...], ...] = ()
    pinned: bool = False


#: Дефолты. Каждый помечен как гипотеза, потому что снят не с живой страницы,
#: а выведен из формы листинговых ключей.
DEFAULTS: dict[str, SelectorMap] = {
    "ozon": SelectorMap(
        marketplace="ozon",
        # [гипотеза] У листинга ключи содержат "tile"/"searchresult"
        # (ozon.py:114), у карточки — другие. Имена ниже правдоподобны,
        # потому что Ozon называет виджеты по назначению, но не проверены.
        name_widgets=("webProductHeading", "webProductMainWidget"),
        seller_widgets=("webCurrentSeller", "webSellerInfo"),
        seller_paths=(("name",), ("seller", "name"), ("title",)),
    ),
    "wb": SelectorMap(
        marketplace="wb",
        # [репо] supplier наблюдается в каталожном объекте, wildberries.py:300,
        # где он кладётся в raw и теряется. Есть ли он в ответе карточки той
        # же формой — гипотеза.
        seller_paths=(("supplier",), ("supplierName",)),
    ),
    "ym": SelectorMap(
        marketplace="ym",
        # [гипотеза] Корень состояния у карточки отличается от листингового
        # {"widgets": ...} из фикстур репозитория.
        seller_widgets=("DefaultOffer", "CurrentOffer", "ShopInfo"),
        seller_paths=(("shop", "name"), ("shopName",), ("seller", "name")),
    ),
}


class Selectors:
    """Реестр с горячей перезагрузкой."""

    def __init__(self, initial: dict[str, SelectorMap] | None = None) -> None:
        self._maps: dict[str, SelectorMap] = dict(initial or DEFAULTS)

    def get(self, mp: str) -> SelectorMap:
        return self._maps.get(mp, SelectorMap(marketplace=mp))

    def pin(self, mp: str, **fields: Any) -> None:
        """Записать снятый с живой страницы путь."""
        cur = self.get(mp)
        merged = {
            "marketplace": mp,
            "name_widgets": fields.get("name_widgets", cur.name_widgets),
            "seller_widgets": fields.get("seller_widgets", cur.seller_widgets),
            "seller_paths": fields.get("seller_paths", cur.seller_paths),
            "pinned": True,
        }
        self._maps[mp] = SelectorMap(**merged)

    def unpinned(self) -> list[str]:
        """Кто ещё работает на догадках. Это метрика, а не диагностика."""
        return sorted(mp for mp, m in self._maps.items() if not m.pinned)


#: Общий реестр процесса.
REGISTRY = Selectors()
