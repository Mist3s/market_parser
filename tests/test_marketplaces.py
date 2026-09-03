"""Экстракторы трёх маркетплейсов: формы URL, разбор, классификация вердиктов."""

from __future__ import annotations

import json

import pytest

from mktlink.marketplaces import ozon, wb, ym
from mktlink.marketplaces.selectors import DEFAULTS, REGISTRY, Selectors
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from tests.legacy_reference import (
    LEGACY_MARKERS_WORTH_KEEPING,
    legacy_detects_block,
    legacy_looks_like_catalog,
    legacy_parse_ozon_widgets,
)

# --- Ozon ---------------------------------------------------------------------


def test_composer_url_matches_the_repo_form() -> None:
    """Форма из ozon.py:22: путь кодируется целиком."""
    url = ozon.composer_url("/product/smes-1234567890/")
    assert url.startswith("https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=")
    assert "%2Fproduct%2Fsmes-1234567890%2F" in url
    assert "/product/" not in url.split("url=")[1], "путь обязан быть закодирован"


def test_widget_states_are_json_strings_inside_json() -> None:
    """Наблюдение репозитория: значения widgetStates — строки."""
    payload = {"widgetStates": {"webProductHeading-1": json.dumps({"title": "Смесь"})}}
    states = ozon.widget_states(payload)
    assert states["webProductHeading-1"] == {"title": "Смесь"}


def test_ozon_pdp_is_parsed_by_qa_attributes() -> None:
    payload = {
        "widgetStates": {
            "webProductHeading-3311": json.dumps(
                {"textDS": {"text": "Смесь Nutrilon 1", "testInfo": {
                    "automatizationId": "webProductHeading"}}}
            ),
            "webCurrentSeller-4422": json.dumps({"name": "ООО «Ромашка»", "id": 12345}),
        }
    }
    res = ozon.parse_pdp(payload, DEFAULTS["ozon"], sku="1234567890")
    assert res.verdict is Verdict.OK
    assert res.name == "Смесь Nutrilon 1"
    assert res.seller_name == "ООО «Ромашка»"
    assert res.seller_status is SellerStatus.RESOLVED
    assert res.seller_source is not None and res.seller_source.startswith("ozon:state:")


def test_ozon_listing_key_filter_is_not_reused_for_the_card() -> None:
    """Ключи tile/searchresult категорийные; на карточке их нет.

    Эталон — дословная копия фильтра из удалённого скрейпера
    (см. legacy_reference). На карточке он не пропускает ничего, то есть
    не отличает «мы не понимаем эту страницу» от «товаров не найдено».
    """
    payload = {
        "widgetStates": {
            "webProductHeading-3311": json.dumps(
                {"textDS": {"text": "Смесь", "testInfo": {
                    "automatizationId": "webProductHeading"}}}
            )
        }
    }
    assert legacy_parse_ozon_widgets(payload) == 0, "категорийный фильтр карточку не видит"
    assert ozon.parse_pdp(payload, DEFAULTS["ozon"], sku="1").name == "Смесь"


def test_the_legacy_key_filter_only_ever_matched_listing_keys() -> None:
    """Показываем, что фильтр не сломан, а просто про другую страницу."""
    listing = {"widgetStates": {"tileGridDesktop-1": json.dumps({"items": [{}, {}]})}}
    assert legacy_parse_ozon_widgets(listing) == 2, "на листинге он работает"


def test_an_empty_composer_answer_is_a_silent_block_not_a_missing_product() -> None:
    assert ozon.classify_response("x" * 100, {"widgetStates": {}}) is Verdict.SILENT_EMPTY
    assert ozon.classify_response("x" * 5000, {"nope": 1}) is Verdict.SILENT_EMPTY
    assert ozon.classify_response("x" * 5000, {"widgetStates": {"a": "{}"}}) is None


def test_known_keys_but_no_extractable_fields_is_drift_not_a_block() -> None:
    """Разница несёт деньги: за дрейф адрес не списывается."""
    payload = {"widgetStates": {"webSomethingElse": json.dumps({"unrelated": 1})}}
    res = ozon.parse_pdp(payload, DEFAULTS["ozon"], sku="1")
    assert res.verdict is Verdict.SCHEMA_DRIFT


# --- Wildberries ------------------------------------------------------------------


def test_wb_urls_keep_the_region_pinned() -> None:
    """dest меняет цену и наличие, поэтому дрейфовать ему нельзя."""
    url = wb.card_url("12345678")
    assert f"dest={wb.DEST}" in url and "nm=12345678" in url
    assert wb.DEST == "-1257786", "значение из репозитория"
    assert wb.pdp_url("12345678") == (
        "https://www.wildberries.ru/catalog/12345678/detail.aspx"
    )


def test_wb_seller_comes_as_a_field_the_repo_already_receives() -> None:
    """wildberries.py:300 кладёт supplier в raw и теряет его."""
    payload = {
        "data": {
            "products": [
                {"id": 12345678, "name": "Смесь Nutrilon", "supplier": "ООО «Ромашка»",
                 "supplierId": 999}
            ]
        }
    }
    res = wb.parse_card(payload, DEFAULTS["wb"], nm="12345678")
    assert res.verdict is Verdict.OK
    assert res.name == "Смесь Nutrilon"
    assert res.seller_name == "ООО «Ромашка»"
    assert res.seller_id == "999"


def test_wb_prices_are_units_not_roubles() -> None:
    """wb_units_to_kopecks — тождество; умножать на сто нельзя."""
    item = {"sizes": [{"price": {"basic": 105230, "product": 99900}}]}
    assert wb.price_kopecks(item) == 99900


def test_wb_answer_without_our_item_is_not_found_not_a_block() -> None:
    """Ответ пришёл, товары есть — адрес отработал."""
    payload = {"data": {"products": [{"id": 999, "name": "Другой"}]}}
    assert wb.parse_card(payload, DEFAULTS["wb"], nm="12345678").verdict is Verdict.NOT_FOUND


def test_wb_empty_products_is_a_silent_block() -> None:
    assert wb.parse_card({"data": {"products": []}}, DEFAULTS["wb"], nm="1").verdict is (
        Verdict.SILENT_EMPTY
    )
    assert wb.classify_response({"nope": 1}) is Verdict.SILENT_EMPTY


# --- Яндекс.Маркет --------------------------------------------------------------------


def test_region_is_forced_on_every_outbound() -> None:
    assert "lr=213" in ym.fetch_url("/card/slug/4382957723")
    assert "lr=213" in ym.fetch_url("/card/slug/4382957723?sku=1")
    assert ym.REGION_ID == 213


def _pdp(sku: str = "4382957723", *, seller: str = "ООО «Ромашка»", body: str = "") -> str:
    state = json.dumps(
        {"widgets": {"DefaultOffer": {"x": {"skuId": sku, "shop": {"name": seller, "id": 12345}}}}},
        ensure_ascii=False,
    )
    return (
        f'<html><head><meta property="og:url" content="https://market.yandex.ru/card/s/{sku}">'
        f"</head><body><h1>Пюре Semper 4 овоща</h1>"
        f"<script>{state}</script>{body}{'x' * 20000}</body></html>"
    )


def test_ym_card_yields_the_anchored_offers_seller() -> None:
    res = ym.parse_pdp(_pdp(), DEFAULTS["ym"], anchor_ids={"4382957723"})
    assert res.verdict is Verdict.OK
    assert res.name == "Пюре Semper 4 овоща"
    assert res.seller_name == "ООО «Ромашка»"


def test_ym_captcha_is_recognised_before_anything_else() -> None:
    """Порядок инвертирован против репозитория: сперва маркеры."""
    html = '<html><body>showcaptcha</body></html>' + "x" * 30000
    assert ym.classify_response(html, anchor_ids={"1"}) is Verdict.CAPTCHA


def test_legacy_guard_short_circuited_the_marker_scan() -> None:
    """Ранний выход при application/ld+json закорачивал всю проверку.

    Настоящая карточка эту подстроку содержит штатно, значит сканирование
    антибот-маркеров не выполнялось НИКОГДА, и проверку нельзя было бы
    провалидировать тестом: невозможно отличить «маркеры работают» от
    «guard закоротил».
    """
    challenge = "<html>application/ld+json showcaptcha</html>"
    assert legacy_looks_like_catalog(challenge), "ранний выход срабатывает"
    assert not legacy_detects_block(challenge), "и челлендж проходит как нормальная страница"
    assert ym.classify_response(challenge, anchor_ids={"1"}) is Verdict.CAPTCHA


def test_the_legacy_marker_list_is_field_intelligence_worth_inheriting() -> None:
    """Список маркеров — единственная часть, невыводимая из первых принципов."""
    inherited = {m for m in LEGACY_MARKERS_WORTH_KEEPING if "captcha" in m}
    assert inherited, "маркеры капчи унаследованы"
    assert legacy_detects_block("<html>вы не робот</html>"), "без раннего выхода он работал"


def test_a_page_without_identity_is_a_silent_block() -> None:
    assert ym.classify_response("<html>пусто</html>", anchor_ids={"1"}) is Verdict.SILENT_EMPTY


def test_identity_for_another_product_is_drift_not_a_block() -> None:
    html = _pdp("9999999999")
    assert ym.classify_response(html, anchor_ids={"4382957723"}) is Verdict.SCHEMA_DRIFT


def test_positive_absence_marker_is_not_found() -> None:
    html = _pdp(body="<div>Страница не найдена</div>")
    assert ym.classify_response(html, anchor_ids={"4382957723"}) is Verdict.NOT_FOUND


def test_a_card_with_no_offers_is_a_complete_answer_not_a_failure() -> None:
    html = (
        '<html><head><meta property="og:url" content="https://market.yandex.ru/card/s/1">'
        "</head><body><h1>Товар</h1>нет в продаже" + "x" * 30000 + "</body></html>"
    )
    res = ym.parse_pdp(html, DEFAULTS["ym"], anchor_ids={"1"})
    assert res.verdict is Verdict.OK
    assert res.seller_status is SellerStatus.NO_OFFERS


def test_blocked_and_drift_are_structurally_disjoint_on_ym() -> None:
    """Заблокированная страница не несёт og:url с нашим id — по построению."""
    blocked = "<html>showcaptcha</html>" + "x" * 30000
    drift = _pdp("4382957723", seller="").replace('"name": ""', '"nope": ""')
    assert ym.classify_response(blocked, anchor_ids={"4382957723"}) is Verdict.CAPTCHA
    assert ym.identity(blocked) is None
    assert ym.identity(drift) is not None


# --- селекторы ---------------------------------------------------------------------------


def test_defaults_are_declared_unpinned() -> None:
    """Пока путь не снят с живой страницы, лейн честно отдаёт unknown_layout."""
    s = Selectors()
    assert set(s.unpinned()) == {"ozon", "wb", "ym"}
    s.pin("ym", seller_paths=(("shop", "name"),))
    assert "ym" not in s.unpinned()


def test_registry_is_shared_and_hot_reloadable() -> None:
    assert REGISTRY.get("ozon").marketplace == "ozon"
    assert REGISTRY.get("detmir").seller_paths == (), "неизвестный — пустая карта, не ошибка"


@pytest.mark.parametrize("mp", ["ozon", "wb", "ym"])
def test_every_marketplace_has_a_default_map(mp: str) -> None:
    assert DEFAULTS[mp].marketplace == mp
