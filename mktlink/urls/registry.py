"""Закрытый литеральный allowlist трёх маркетплейсов.

Ни одного wildcard-поддомена, и это не педантизм. Защита от DNS rebinding на
прямых хопах опирается на то, что резолвимое имя выбираем **мы**, а не
отправитель ссылки: при правиле ``.ozon.ru`` метку поддомена выбирал бы он,
и весь аргумент рассыпался бы.

Порядок проверок фиксирован: ``pdp`` → ``shortlink`` → ``not_pdp``.
``not_pdp`` — классификатор для текста ошибки, **а не гейт**, и порядок здесь
несущий: репозиторная PDP-форма Wildberries ``/catalog/12345678/detail.aspx``
матчит и ``pdp``, и ``not_pdp`` (потому что ``12345678`` подходит под
``[a-z0-9-]+``), поэтому при обратном порядке отвергалась бы **каждая** ссылка
на товар WB.

Раскручиваются только собственные шорт-формы маркетплейсов. Универсальные
шортенеры (``clck.ru``, ``ya.cc``, ``bit.ly``) отвергаются: у них нет
владеющей строки реестра, а значит нет и заключения цепочки в один
маркетплейс — без него редирект-раскрутка превращается в открытый прокси.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Доказательная база у форм разная, и это записано у каждой:
#:
#: * ``[репо]`` — наблюдается в файлах ``market_parser``;
#: * ``[выведено]`` — следует из проверенного механизма;
#: * ``[гипотеза]`` — требует пиннинга замером Phase 0.


@dataclass(frozen=True, slots=True)
class HostRule:
    marketplace: str
    hosts: frozenset[str]
    pdp: tuple[re.Pattern[str], ...]
    shortlink: tuple[re.Pattern[str], ...]
    not_pdp: tuple[re.Pattern[str], ...]
    #: Query-параметры, которые ВЫБИРАЮТ ОФФЕР, а оффер определяет продавца.
    #: Сбросить их значило бы вернуть продавца другого оффера — молчаливая
    #: порча целостности, которую клиент обнаружить не может.
    offer_params: frozenset[str]


REGISTRY: Final[dict[str, HostRule]] = {
    "ozon": HostRule(
        marketplace="ozon",
        hosts=frozenset({"www.ozon.ru", "ozon.ru"}),
        pdp=(
            re.compile(r"^/product/(?P<slug>[^/]*?)-(?P<sku>\d{6,12})/?$"),
            re.compile(r"^/product/(?P<sku>\d{6,12})/?$"),
        ),
        # [гипотеза, Phase 0] — форму надо снять шером карточки из приложения.
        shortlink=(re.compile(r"^/t/(?P<code>[A-Za-z0-9]{4,16})/?$"),),
        not_pdp=(re.compile(r"^/(category|search|seller|highlight|brand|club|modal)"),),
        offer_params=frozenset({"sku"}),
    ),
    "wb": HostRule(
        marketplace="wb",
        hosts=frozenset({"www.wildberries.ru", "wildberries.ru"}),
        # [репо] wildberries.py:293 строит именно эту форму.
        pdp=(re.compile(r"^/catalog/(?P<nm>\d{5,12})/detail\.aspx$"),),
        # Форма шеринга WB не измерена, поэтому НИ ОДНОЙ: пустой кортеж
        # означает «в раскрутку не допускается ничего», а не «допускается всё».
        shortlink=(),
        not_pdp=(
            re.compile(r"^/catalog/[a-z0-9-]+/"),
            re.compile(r"^/(seller|brands|promotions)"),
        ),
        offer_params=frozenset(),
    ),
    "ym": HostRule(
        marketplace="ym",
        hosts=frozenset({"market.yandex.ru"}),
        pdp=(
            # [репо] селектор a[href*="/card/"] в html_extractors.py:180, и в
            # фикстуре число в href равно skuId.
            re.compile(r"^/card/(?P<slug>[^/]+)/(?P<sku_id>\d{5,14})/?$"),
            re.compile(r"^/card/(?P<sku_id>\d{5,14})/?$"),
            # [выведено] из product_href_patterns в yandex_market.py:18.
            re.compile(r"^/product--(?P<slug>[^/]+)/(?P<product_id>\d{5,14})/?$"),
            re.compile(r"^/product/(?P<product_id>\d{5,14})/?$"),
            # [гипотеза] оффер конкретного продавца.
            re.compile(r"^/offer/(?P<ware_md5>[A-Za-z0-9_-]{20,26})/?$"),
        ),
        # [гипотеза, Phase 0]
        shortlink=(re.compile(r"^/cc/(?P<code>[A-Za-z0-9_-]{4,24})/?$"),),
        not_pdp=(re.compile(r"^/(catalog--|search|promo|special|brands--|lists)/?"),),
        # На Я.Маркете одна карточка агрегирует офферы МНОГИХ продавцов,
        # поэтому эти параметры — не шум, а выбор продавца.
        offer_params=frozenset({"sku", "offerid", "do-waremd5"}),
    ),
}

#: Все допустимые хосты, плоским множеством — для проверки на каждом хопе.
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    host for rule in REGISTRY.values() for host in rule.hosts
)


@dataclass(frozen=True, slots=True)
class Match:
    """Результат разбора пути."""

    marketplace: str
    kind: str  # "pdp" | "shortlink"
    ids: dict[str, str]

    @property
    def is_pdp(self) -> bool:
        return self.kind == "pdp"

    @property
    def is_shortlink(self) -> bool:
        return self.kind == "shortlink"


class UnknownHost(ValueError):
    """Хост вне литерального allowlist'а."""


class NotAProductUrl(ValueError):
    """Хост наш, но путь — не товар."""

    def __init__(self, marketplace: str, path: str, *, classified: str | None) -> None:
        self.marketplace = marketplace
        self.path = path
        #: Что именно это за путь, если удалось узнать: для текста ошибки.
        self.classified = classified
        super().__init__(
            f"{marketplace}: path {path!r} is not a product URL"
            + (f" (looks like {classified})" if classified else "")
        )


def rule_for_host(host: str) -> HostRule:
    for rule in REGISTRY.values():
        if host in rule.hosts:
            return rule
    raise UnknownHost(f"host not allowed: {host!r}")


def match_path(host: str, path: str) -> Match:
    """Разобрать путь. Порядок проверок фиксирован и обоснован в docstring модуля."""
    rule = rule_for_host(host)

    for pat in rule.pdp:
        m = pat.match(path)
        if m is not None:
            return Match(rule.marketplace, "pdp", {k: v for k, v in m.groupdict().items() if v})

    for pat in rule.shortlink:
        m = pat.match(path)
        if m is not None:
            return Match(
                rule.marketplace, "shortlink", {k: v for k, v in m.groupdict().items() if v}
            )

    classified = None
    for pat in rule.not_pdp:
        if pat.match(path):
            classified = "a category, search or promo page"
            break
    raise NotAProductUrl(rule.marketplace, path, classified=classified)


def unwind_eligible(host: str, path: str) -> bool:
    """Допуск в редирект-раскрутку — единственная формулировка на весь пакет.

    URL входит в раскрутку тогда и только тогда, когда его хост в литеральном
    allowlist'е И путь матчит ``shortlink``-паттерн этого хоста. Матчить
    ``pdp`` раскручиваемый путь **не обязан** — в этом весь смысл: короткая
    ссылка из приложения числового id не содержит.
    """
    try:
        rule = rule_for_host(host)
    except UnknownHost:
        return False
    return any(pat.match(path) for pat in rule.shortlink)


def offer_params(marketplace: str) -> frozenset[str]:
    return REGISTRY[marketplace].offer_params
