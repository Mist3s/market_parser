"""Реестр обычных магазинов: хост → как читать карточку. Одна строка на магазин.

Парсеров по магазинам здесь нет и не должно появиться. Все четырнадцать
известных сайтов читаются одним экстрактором (:mod:`mktlink.shops.extract`),
а запись в реестре отвечает на четыре вопроса:

1. **Какой хост** и как называется магазин в ответе.
2. **Какой путь — карточка.** Категории, поиск и главная отвергаются до
   сети: ``/catalog/puer/`` у Мойчая тоже имеет ``<h1>``, и без этой
   проверки сервис отдал бы название категории как название чая.
3. **Как получить HTML, если сайт — SPA.** Teaworkshop отдаёт пустую
   оболочку Vue и заполняет её скриптом; но объявляет
   ``<meta name="fragment" content="!">``, и по ``?_escaped_fragment_=``
   сервер отдаёт отрендеренную страницу с ``<h1>`` (ЗАМЕР 2026-09-18).
4. **Что входит в канонический URL.** У Чайной Линии ``?oid=`` выбирает
   вариант товара; остальные параметры (utm и прочее) отбрасываются.

Добавить магазин — дописать одну строку в ``SHOPS`` и одну карточку в
``tests/test_shops_extract.py``. Если общий порядок источников на новом
сайте даёт мусор, порядок переопределяется полем ``name_sources``, а не
новым модулем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode

from mktlink.shops.extract import DEFAULT_NAME_SOURCES

Prerender = Literal["none", "escaped_fragment"]

#: Признак карточки в HTML для магазинов, у которых путь категории и товара
#: неразличим по форме. Проверяется ПОСЛЕ загрузки, поэтому не заменяет
#: ``product_path``, а дополняет его.
PRODUCT_MARKER: Final[re.Pattern[str]] = re.compile(
    r"schema\.org/Product\b|\"@type\"\s*:\s*\"Product\"|og:type\"?\s+content=\"product",
    re.I,
)


def _p(pattern: str) -> re.Pattern[str]:
    """Путь целиком, без учёта регистра."""
    return re.compile(rf"^{pattern}$", re.I)


ANY_PATH: Final[re.Pattern[str]] = _p(r"/.+")


@dataclass(frozen=True, slots=True)
class Shop:
    #: Хост без ``www.``; ``www.`` принимается автоматически.
    host: str
    #: Как магазин называется в ответе. ``None`` — взять ``og:site_name``.
    name: str | None
    product_path: re.Pattern[str]
    #: Для диагностики и статистики дрейфа, на разбор не влияет.
    platform: str = "unknown"
    aliases: tuple[str, ...] = ()
    prerender: Prerender = "none"
    name_sources: tuple[str, ...] = DEFAULT_NAME_SOURCES
    #: Параметры запроса, входящие в канонический URL.
    keep_query: tuple[str, ...] = ()
    #: Дополнительная проверка «это карточка» по телу страницы.
    product_marker: re.Pattern[str] | None = None

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset({self.host, f"www.{self.host}", *self.aliases})

    def is_product_path(self, path: str) -> bool:
        return self.product_path.match(path) is not None

    def canonical(self, path: str, query: str) -> str:
        pairs = parse_qsl(query, keep_blank_values=False)
        kept = [(k, v) for k, v in pairs if k in self.keep_query]
        tail = f"?{urlencode(kept)}" if kept else ""
        return f"https://{self.host}{path}{tail}"

    def fetch_url(self, canonical: str) -> str:
        """Что реально запрашивать. Для SPA — вариант с prerender-параметром."""
        if self.prerender == "escaped_fragment":
            sep = "&" if "?" in canonical else "?"
            return f"{canonical}{sep}_escaped_fragment_="
        return canonical

    @classmethod
    def generic(cls, host: str) -> Shop:
        """Незнакомый хост, разрешённый настройкой: любой путь, общий порядок."""
        return cls(host=host, name=None, product_path=ANY_PATH, platform="generic")


SHOPS: Final[tuple[Shop, ...]] = (
    Shop("moschaitorg.ru", "Мосчайторг", _p(r"/product/[^/]+/?"), platform="bitrix"),
    Shop("realchinatea.ru", "RealChinaTea", _p(r"/shop/[^/]+/?"), platform="tilda"),
    # Категория и товар — оба под /catalog/ и оба бывают трёхсегментными
    # (/catalog/puer/shen_puer_pressovannyj — категория,
    # /catalog/herbal-tea-moychaycom/travyanoy-sbor-… — товар), поэтому путь
    # даёт только грубый фильтр, а карточку подтверждает маркер.
    Shop(
        "moychay.ru",
        "Мойчай.ру",
        _p(r"/catalog/(?!main/?$)[^/]+(?:/[^/]+){1,2}/?"),
        platform="inertia",
        product_marker=PRODUCT_MARKER,
    ),
    Shop(
        "artoftea.ru",
        "Art of Tea",
        _p(r"/[^/]+/[^/]+/[^/]+/?"),
        platform="opencart",
        product_marker=PRODUCT_MARKER,
    ),
    Shop(
        "teaworkshop.ru",
        "Чайная мастерская",
        _p(r"/product/[^/]+/?"),
        platform="vue-spa",
        prerender="escaped_fragment",
    ),
    Shop("imperatormin.ru", "Император Минь", _p(r"/product/[^/]+/?")),
    Shop(
        "chaline.ru",
        "Чайная Линия",
        _p(r"/catalog/(?:[^/]+/)+\d+/?"),
        platform="bitrix",
        keep_query=("oid",),
    ),
    Shop("teaboom.ru", "Чайный Бум", _p(r"/product/[^/]+/?")),
    Shop("aromatchaya.ru", "Аромат Чая", _p(r"/product/[^/]+/?"), platform="woocommerce"),
    Shop("chayniy-put.ru", "Чайный Путь", _p(r"/tproduct/[^/]+/?"), platform="tilda"),
    Shop(
        "kofcheg.ru",
        "Кофчег",
        _p(r"/[^/]+/[^/]+/[^/]+/?"),
        platform="opencart",
        product_marker=PRODUCT_MARKER,
    ),
    Shop("teaguru.ru", "TeaGuru", _p(r"/product/[^/]+/?"), platform="shop-script"),
    Shop("tiptoptea.ru", "TipTopTea", _p(r"/catalog/[^/]+/[^/]+/?"), platform="bitrix"),
    Shop("chaekshop.ru", "Чаёк", _p(r"/product/[^/]+/?"), platform="nuxt"),
)

_BY_HOST: Final[dict[str, Shop]] = {h: s for s in SHOPS for h in s.hosts}

if len(_BY_HOST) != sum(len(s.hosts) for s in SHOPS):
    raise RuntimeError("shop registry: duplicate host")


def lookup(host: str) -> Shop | None:
    return _BY_HOST.get(host.casefold())


__all__ = ["ANY_PATH", "PRODUCT_MARKER", "SHOPS", "Shop", "lookup"]
