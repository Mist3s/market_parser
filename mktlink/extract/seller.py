"""Извлечение продавца. Якорный поиск, и НИКОГДА не догадка.

Почему это отдельный модуль с собственной дисциплиной. На Ozon и Я.Маркете
одна карточка агрегирует офферы разных продавцов, а страница вдобавок несёт
карусели рекомендаций, «похожие товары» и блоки аксессуаров — и у каждого
такого блока есть своё название и своя цена.

Репозиторный ``html_extractors._find_yandex_offer`` рекурсивно возвращает
ПЕРВЫЙ попавшийся словарь с непустым ``title`` и ``price.value``. На листинге
это корректно: область поиска уже сужена селектором до одного сниппета. На
карточке области поиска нет вовсе, и та же функция вернула бы продавца
случайного рекомендованного товара — ответ, который выглядит валидным и
который клиент не может опровергнуть.

Отсюда правило: кандидат принимается, только если он привязан к ЗАПРОШЕННОМУ
идентификатору. Не нашли якорь — возвращаем ``None`` и говорим почему.

Три вещи, которые запрещены явно:

* Откат на бренд, производителя, ``og:site_name`` или имя маркетплейса.
  Вернуть «Nutrilon» продавцом товара, который продаёт ООО «Ромашка», хуже,
  чем вернуть ``null``.
* Подстановка другого оффера, когда запрошенный исчез. Это тихая неправда.
* Выбор «лучшего» кандидата, когда их несколько и они равнозначны.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mktlink.extract.jsonscan import walk
from mktlink.extract.normalize import normalize_text
from mktlink.marketplaces.verdict import SellerStatus

#: Грамматика происхождения значения. Одна форма на весь проект: ``<mp>:<tier>:<path>``.
#: Проверка «якорный ли путь» смотрит на СЕГМЕНТ УРОВНЯ, а не на всю строку —
#: сравнение целой строки с префиксами уровней не совпадало бы никогда, и
#: продавец был бы null на каждом запросе при зелёных метриках лестницы.
SOURCE_RE = re.compile(r"^(?P<mp>ozon|wb|ym):(?P<tier>[a-z_]+(?::[a-z_]+)?):(?P<path>.+)$")

#: Уровни, которые считаются якорными.
ANCHORED_TIERS: tuple[str, ...] = ("state", "dom:offer", "legal_block")

#: Имена полей, где продавец вообще может лежать.
_SELLER_KEY = re.compile(
    r"^(seller|supplier|merchant|shop|vendor|store)(_?(name|title|legal_?name))?$", re.I
)

#: Пути рекомендаций и каруселей. Кандидат, лежащий под таким сегментом,
#: отбрасывается независимо от того, насколько он похож на продавца.
RECO_PATH = re.compile(
    r"(recommend|similar|accessor|also[_-]?bought|viewed|carousel|cross[_-]?sell|promo[_-]?block)",
    re.I,
)

#: Имена самих маркетплейсов. Нужны не чтобы отбросить их, а чтобы ОТЛИЧИТЬ
#: «продавец действительно маркетплейс» от «мы не смогли разрешить продавца».
_FIRST_PARTY = {
    "ozon": {"ozon", "озон", "ozon.ru", "интернет-решения"},
    "wb": {"wildberries", "вайлдберриз", "вб"},
    "ym": {"яндекс.маркет", "яндекс маркет", "yandex.market", "маркет", "яндекс"},
}

_LEGAL_FORM = re.compile(r"\b(ООО|ИП|АО|ЗАО|ПАО|ОАО|НАО)\b")


def make_source(mp: str, tier: str, path: tuple[str, ...] | str) -> str:
    """Собрать строку происхождения по единой грамматике."""
    p = path if isinstance(path, str) else ".".join(path)
    return f"{mp}:{tier}:{p}"


def is_anchored(source: str) -> bool:
    """Якорный ли путь. Сравнивается сегмент уровня, а не вся строка."""
    m = SOURCE_RE.match(source)
    if m is None:
        return False
    tail = source.split(":", 1)[1]
    return tail.startswith(ANCHORED_TIERS)


@dataclass(frozen=True, slots=True)
class SellerFound:
    name: str | None
    status: SellerStatus
    source: str | None
    seller_id: str | None = None
    legal_name: str | None = None


def classify(
    mp: str,
    name: str | None,
    source: str | None,
    extra: dict[str, Any] | None = None,
) -> SellerFound:
    """Решить, можно ли отдать найденное имя как продавца.

    Возвращает ``UNKNOWN_LAYOUT`` вместо имени всегда, когда путь не якорный:
    неякорное совпадение — это совпадение где-то на странице, а не продавец
    этого оффера.
    """
    extra = extra or {}
    if not name or not source:
        return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, source)

    clean = normalize_text(name)
    if not clean or not (2 <= len(clean) <= 120):
        return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, source)

    if not is_anchored(source):
        return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, source)

    seller_id = extra.get("seller_id") or extra.get("shopId") or extra.get("supplierId")
    legal = extra.get("legal_name")

    if clean.casefold() in _FIRST_PARTY.get(mp, set()):
        # Продавец действительно маркетплейс — это факт, а не наша неудача,
        # и клиент обязан отличать один случай от другого.
        return SellerFound(
            clean,
            SellerStatus.FIRST_PARTY,
            source,
            seller_id=str(seller_id) if seller_id else None,
            legal_name=legal,
        )

    return SellerFound(
        clean,
        SellerStatus.RESOLVED,
        source,
        seller_id=str(seller_id) if seller_id else None,
        legal_name=legal,
    )


def select_anchored(
    state: Any,
    *,
    mp: str,
    anchor_ids: set[str],
    tier: str = "state",
) -> SellerFound:
    """Найти продавца, привязанного к запрошенному идентификатору.

    ``anchor_ids`` — идентификаторы товара из URL. Кандидат принимается,
    только если якорь встречается на его пути или среди его соседей: это и
    отличает продавца нашего оффера от продавца рекомендованного товара.
    """
    best: tuple[int, str, tuple[str, ...], dict[str, Any]] | None = None
    saw_candidate = False

    for path, node in walk(state):
        if not isinstance(node, dict):
            continue
        if any(RECO_PATH.search(seg) for seg in path):
            continue

        value = _seller_value(node)
        if value is None:
            continue
        saw_candidate = True

        score = _score(path, node, anchor_ids)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, value, path, node)

    if best is None:
        # Кандидаты были, но ни один не привязан к нашему товару, — это дрейф
        # разметки, а не блок: страница наша, а путь мы больше не понимаем.
        return SellerFound(
            None,
            SellerStatus.UNKNOWN_LAYOUT if saw_candidate else SellerStatus.NO_OFFERS,
            None,
        )

    _, value, path, node = best
    return classify(mp, value, make_source(mp, tier, path), _neighbours(node))


def _seller_value(node: dict[str, Any]) -> str | None:
    for k, v in node.items():
        if isinstance(v, str) and v.strip() and _SELLER_KEY.fullmatch(k):
            return v
    # Вложенная форма: {"shop": {"name": ...}}
    for k, v in node.items():
        if isinstance(v, dict) and _SELLER_KEY.fullmatch(k):
            for nk in ("name", "title", "legalName", "legal_name"):
                nv = v.get(nk)
                if isinstance(nv, str) and nv.strip():
                    return nv
    return None


def _score(path: tuple[str, ...], node: dict[str, Any], anchor_ids: set[str]) -> int:
    """Оценка кандидата. Якорь обязателен: без него ноль, то есть отказ."""
    anchored = _has_anchor(path, node, anchor_ids)
    if not anchored:
        return 0
    score = 3
    if any("seller" in s.lower() or "supplier" in s.lower() or "shop" in s.lower() for s in path):
        score += 2
    if any(k in node for k in ("id", "shopId", "supplierId", "link", "url", "rating")):
        score += 1
    for v in node.values():
        if isinstance(v, str) and _LEGAL_FORM.search(v):
            score += 2
            break
    return score


def _has_anchor(path: tuple[str, ...], node: dict[str, Any], anchor_ids: set[str]) -> bool:
    if not anchor_ids:
        return False
    if any(seg in anchor_ids for seg in path):
        return True
    for v in node.values():
        if isinstance(v, (str, int)) and str(v) in anchor_ids:
            return True
    return False


def _neighbours(node: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("id", "shopId", "supplierId", "sellerId"):
        if k in node:
            out["seller_id"] = node[k]
            break
    for v in node.values():
        if isinstance(v, str) and _LEGAL_FORM.search(v):
            out["legal_name"] = v
            break
    return out
