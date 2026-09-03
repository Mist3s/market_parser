"""Извлечение продавца: якорь обязателен, догадка запрещена.

Центральный тест здесь — тот, что демонстрирует отказ репозиторной функции
на карточке. Он важнее остальных: без него слой выглядит работающим и молча
возвращает продавца чужого товара.
"""

from __future__ import annotations

import pytest

from mktlink.extract.jsonscan import (
    iter_json_objects,
    iter_script_blobs,
    iter_state_candidates,
    loads_maybe,
    walk,
)
from mktlink.extract.normalize import parse_price_to_kopecks
from mktlink.extract.seller import (
    ANCHORED_TIERS,
    SOURCE_RE,
    classify,
    is_anchored,
    make_source,
    select_anchored,
)
from mktlink.marketplaces.verdict import (
    CHARGES_PROXY,
    SellerStatus,
    Verdict,
    charges_proxy,
)

# --- деньги ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kop"),
    [("19.99", 1999), ("1.15", 115), ("0.29", 29), ("1052", 105200), ("1 052,30", 105230)],
)
def test_prices_are_exact_where_the_repo_loses_a_kopeck(raw: str, kop: int) -> None:
    """Репозиторный int(float(v)*100) даёт 1998 на "19.99". Decimal даёт 1999."""
    assert parse_price_to_kopecks(raw) == kop
    naive = int(float(raw.replace(",", ".").replace(" ", "")) * 100)
    if raw in ("19.99", "1.15", "0.29"):
        assert naive != kop, "иначе тест ничего не доказывает"


# --- сканер JSON --------------------------------------------------------------


def test_script_blobs_are_read_raw_not_through_entity_decoding() -> None:
    """Название с &quot; внутри ломает JSON, если читать раскодированный текст."""
    html = '<script>{"widgets":{"x":{"title":"Смесь &quot;Малыш&quot;"}}}</script>'
    blobs = list(iter_script_blobs(html))
    assert len(blobs) == 1 and "&quot;" in blobs[0]
    objs = list(iter_json_objects(blobs[0]))
    assert objs and objs[0]["widgets"]["x"]["title"] == 'Смесь &quot;Малыш&quot;'


def test_scanner_finds_several_roots() -> None:
    html = (
        '<script>{"collections":{"a":1}}</script>'
        '<script>{"widgetStates":{"b":2}}</script>'
    )
    objs = list(iter_state_candidates(html))
    assert len(objs) == 2


def test_scanner_falls_back_to_raw_text() -> None:
    """Фикстуры репозитория кладут состояние текстовым узлом, не в script."""
    html = '<div data-baobab-name="productSnippet">{"widgets":{"x":{"title":"t"}}}</div>'
    objs = list(iter_state_candidates(html))
    assert objs and "widgets" in objs[0]


def test_scanner_is_bounded() -> None:
    html = "<script>" + '{"widgets":1}' * 500 + "</script>"
    assert len(list(iter_json_objects(html, max_objects=5))) <= 5


def test_ozon_widget_states_are_json_strings_inside_json() -> None:
    """Наблюдение из репозитория: значения widgetStates — строки с JSON."""
    assert loads_maybe('{"a": 1}') == {"a": 1}
    assert loads_maybe({"a": 1}) == {"a": 1}
    assert loads_maybe("not json") is None
    assert loads_maybe(None) is None


def test_walk_yields_paths() -> None:
    paths = {p for p, _ in walk({"a": {"b": [1]}})}
    assert ("a", "b", "0") in paths


# --- грамматика происхождения --------------------------------------------------


def test_source_grammar_is_one_form_and_the_anchored_test_matches_it() -> None:
    """Дефект, из-за которого продавец был бы null на каждом запросе.

    Проверка сравнивала ВСЮ строку с префиксами уровней. Строка начинается с
    имени маркетплейса, поэтому не совпадала никогда.
    """
    src = make_source("ym", "state", ("widgets", "@marketfront/DefaultOffer", "x", "shop", "name"))
    assert src == "ym:state:widgets.@marketfront/DefaultOffer.x.shop.name"
    assert SOURCE_RE.match(src)
    assert is_anchored(src), "наивное startswith по всей строке дало бы False"
    # А наивная проверка действительно провалилась бы:
    assert not src.startswith(ANCHORED_TIERS)


@pytest.mark.parametrize(
    ("tier", "anchored"),
    [
        ("state", True),
        ("dom:offer", True),
        ("legal_block", True),
        ("jsonld", False),
        ("dom", False),
    ],
)
def test_only_anchored_tiers_are_accepted(tier: str, anchored: bool) -> None:
    assert is_anchored(make_source("ozon", tier, ("a", "b"))) is anchored


def test_malformed_source_is_not_anchored() -> None:
    for bad in ("", "nonsense", "detmir:state:x", "state:x"):
        assert not is_anchored(bad)


# --- классификация ------------------------------------------------------------


def test_anchored_third_party_seller_resolves() -> None:
    src = make_source("ym", "state", ("widgets", "DefaultOffer", "shop", "name"))
    got = classify("ym", 'ООО «Ромашка»', src, {"shopId": 12345})
    assert got.status is SellerStatus.RESOLVED
    assert got.name == "ООО «Ромашка»"
    assert got.seller_id == "12345"


def test_marketplace_as_seller_is_a_fact_not_a_failure() -> None:
    """Клиент обязан отличать «продавец — Ozon» от «мы не разрешили продавца»."""
    src = make_source("ozon", "state", ("widgetStates", "webCurrentSeller", "name"))
    got = classify("ozon", "Ozon", src, {})
    assert got.status is SellerStatus.FIRST_PARTY
    assert got.name == "Ozon"

    unresolved = classify("ozon", "Ozon", "ozon:jsonld:offers.0.seller.name", {})
    assert unresolved.status is SellerStatus.UNKNOWN_LAYOUT
    assert unresolved.name is None


def test_non_anchored_match_is_refused_even_when_it_looks_perfect() -> None:
    got = classify("ym", 'ООО «Ромашка»', "ym:jsonld:offers.0.seller.name", {})
    assert got.name is None and got.status is SellerStatus.UNKNOWN_LAYOUT


def test_absent_or_absurd_names_are_refused() -> None:
    src = make_source("ym", "state", ("a",))
    for bad in (None, "", "  ", "x", "y" * 200):
        assert classify("ym", bad, src, {}).name is None


# --- якорный выбор: центральный случай -----------------------------------------

#: Карточка: наш оффер плюс карусель рекомендаций. Ровно та форма, на которой
#: репозиторная функция «первый подходящий» возвращает не того продавца.
PDP_STATE = {
    "widgets": {
        "recommendations": {
            "items": [
                {
                    "productId": "9999999999",
                    "title": "Совсем другой товар",
                    "price": {"value": "100"},
                    "shop": {"name": 'ООО «Чужой Продавец»', "id": 777},
                }
            ]
        },
        "DefaultOffer": {
            "x": {
                "skuId": "4382957723",
                "title": "Пюре Semper",
                "price": {"value": "1052"},
                "shop": {"name": 'ООО «Ромашка»', "id": 12345},
            }
        },
    }
}


def test_anchored_selection_returns_our_offers_seller_not_a_recommendations() -> None:
    got = select_anchored(PDP_STATE, mp="ym", anchor_ids={"4382957723"})
    assert got.status is SellerStatus.RESOLVED
    assert got.name == "ООО «Ромашка»"
    assert "Чужой" not in (got.name or "")


def test_repo_first_match_heuristic_would_have_returned_the_wrong_seller() -> None:
    """Демонстрация отказа, ради которой функция и заменена.

    Воспроизводим репозиторную логику: рекурсивно первый словарь с title и
    price.value. На листинге она корректна, на карточке — нет.
    """

    def repo_first_match(value):
        if isinstance(value, dict):
            if value.get("title") and isinstance(value.get("price"), dict):
                if value["price"].get("value"):
                    return value
            for child in value.values():
                found = repo_first_match(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = repo_first_match(child)
                if found:
                    return found
        return None

    picked = repo_first_match(PDP_STATE)
    assert picked is not None
    assert picked["shop"]["name"] == "ООО «Чужой Продавец»", "именно это мы и чиним"


def test_recommendation_paths_are_skipped_entirely() -> None:
    only_reco = {"widgets": {"recommendations": {"items": [
        {"skuId": "4382957723", "shop": {"name": "ООО «Чужой»"}}
    ]}}}
    got = select_anchored(only_reco, mp="ym", anchor_ids={"4382957723"})
    assert got.name is None


def test_no_anchor_means_no_seller_not_a_best_guess() -> None:
    got = select_anchored(PDP_STATE, mp="ym", anchor_ids={"1111111111"})
    assert got.name is None
    assert got.status in (SellerStatus.UNKNOWN_LAYOUT, SellerStatus.NO_OFFERS)


def test_a_card_without_offers_is_answered_not_refused() -> None:
    got = select_anchored({"widgets": {}}, mp="ym", anchor_ids={"4382957723"})
    assert got.status is SellerStatus.NO_OFFERS


def test_legal_name_is_picked_up_from_neighbours() -> None:
    state = {
        "offer": {
            "skuId": "1",
            "sellerName": "Ромашка",
            "legalTitle": 'ООО "Ромашка"',
            "id": 5,
        }
    }
    got = select_anchored(state, mp="ym", anchor_ids={"1"})
    assert got.status is SellerStatus.RESOLVED
    assert got.legal_name == 'ООО "Ромашка"'


# --- вердикты -----------------------------------------------------------------


def test_schema_drift_never_charges_the_proxy() -> None:
    """Маркетплейс переименовал ключ — виноват наш парсер, а не адрес."""
    assert Verdict.SCHEMA_DRIFT not in CHARGES_PROXY
    assert not charges_proxy(Verdict.SCHEMA_DRIFT, egress="proxy")


def test_budget_exhaustion_never_charges_the_proxy() -> None:
    assert not charges_proxy(Verdict.BUDGET_EXHAUSTED, egress="proxy")


def test_observation_not_made_through_the_proxy_is_never_evidence() -> None:
    """Раскрутка идёт прямым егрессом; челлендж в ней не списывает адрес."""
    assert charges_proxy(Verdict.CAPTCHA, egress="proxy")
    assert not charges_proxy(Verdict.CAPTCHA, egress="direct")
    assert not charges_proxy(Verdict.SILENT_EMPTY, egress="direct")
    assert not charges_proxy(Verdict.SILENT_EMPTY, egress="forge")


def test_the_charging_list_is_positive_so_new_verdicts_are_safe_by_default() -> None:
    for v in Verdict:
        if v not in CHARGES_PROXY:
            assert not charges_proxy(v, egress="proxy")
