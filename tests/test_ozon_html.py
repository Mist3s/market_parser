"""HTML-лейн Ozon: три замеренные формы ответа и якорный продавец.

Все три формы получены 2026-09-03 на одной и той же карточке. Их различение
важнее самого разбора: за блок адрес штрафуется, за незавершённое
JS-рукопожатие — нет, а за дрейф разметки не штрафуется и не заменяется.
"""

from __future__ import annotations

import pathlib

from mktlink.marketplaces import ozon
from mktlink.marketplaces.verdict import CHARGES_PROXY, SellerStatus, Verdict

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SKU = "3160461596"

#: Форма 1. Страница блока: датацентровый адрес в блок-листе. 5 221 байт,
#: заголовок «Похоже, нет соединения», челлендж НЕ предложен.
BLOCK_PAGE = (
    "<html><head><title>Похоже, нет соединения</title></head>"
    "<body>Проверьте подключение</body></html>" + "<!-- " + "x" * 5000 + " -->"
)

#: Форма 2. Интерстишл JS-рукопожатия: адрес принят, но JS не выполнен.
#: 13 272 байта, заголовок «Происходит перенаправление».
INTERSTITIAL = (
    '<html><head><meta name="robots" content="noindex">'
    "<title>Происходит перенаправление</title></head><body></body></html>"
    + "<!-- " + "y" * 13000 + " -->"
)


def _fixture() -> str:
    return (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")


# --- классификация ------------------------------------------------------------


def test_block_page_is_a_challenge_even_at_status_200() -> None:
    """Через скрейпинг-API страница блока приходит с кодом 200.

    Это и есть причина, по которой подписи тела здесь РЕШАЮТ, а не только
    диагностируют: статус приходит от поставщика, а не от Ozon.
    """
    assert ozon.classify_html(BLOCK_PAGE, anchor_ids={SKU}, status=200) is Verdict.CAPTCHA
    assert ozon.classify_html(BLOCK_PAGE, anchor_ids={SKU}, status=403) is Verdict.CAPTCHA
    assert Verdict.CAPTCHA in CHARGES_PROXY


def test_interstitial_is_not_a_block_and_does_not_charge() -> None:
    """«Происходит перенаправление» — адрес принят, не выполнен только JS.

    Штрафовать за это адрес нельзя: он как раз показал, что работает.
    """
    verdict = ozon.classify_html(INTERSTITIAL, anchor_ids={SKU}, status=200)
    assert verdict is Verdict.CLIENT_RENDERED
    assert verdict not in CHARGES_PROXY


def test_real_card_is_not_classified_at_all() -> None:
    assert ozon.classify_html(_fixture(), anchor_ids={SKU}, status=200) is None


def test_status_still_decides_before_the_body() -> None:
    assert ozon.classify_html(_fixture(), anchor_ids={SKU}, status=429) is Verdict.HTTP_429
    assert ozon.classify_html(_fixture(), anchor_ids={SKU}, status=503) is Verdict.UPSTREAM_ERROR


def test_another_products_page_is_drift_not_a_block() -> None:
    """Страница наша, но про другой товар: за это адрес не штрафуется."""
    verdict = ozon.classify_html(_fixture(), anchor_ids={"9999999999"}, status=200)
    assert verdict is Verdict.SCHEMA_DRIFT
    assert verdict not in CHARGES_PROXY


def test_identity_comes_from_og_url() -> None:
    ident = ozon.identity_html(_fixture())
    assert ident is not None and SKU in ident
    assert ozon.identity_html(BLOCK_PAGE) is None


# --- разбор --------------------------------------------------------------------


def test_name_and_anchored_seller() -> None:
    r = ozon.parse_pdp_html(_fixture(), anchor_ids={SKU})

    assert r.verdict is Verdict.OK
    assert r.name == 'Китайский чай Шу Пуэр "Цзинь Я Сяо Пин", "Золотые почки", сырье 2009г, 100гр'
    assert "купить на OZON" not in r.name, "хвост заголовка страницы отрезается"
    assert r.seller_name == "Шуняня"
    assert r.seller_id == "shunana"
    assert r.seller_status is SellerStatus.RESOLVED
    assert r.seller_source is not None and "seller" in r.seller_source
    assert r.complete


def test_seller_needs_both_anchors() -> None:
    """Один атрибут — совпадение случайное, два — нет.

    Ссылка без ``title`` продавцом не считается: имя пришлось бы брать из
    текста ссылки, который на карточке Ozon может быть и «Перейти в магазин».
    """
    html = _fixture().replace('<a title="Шуняня" href="https://www.ozon.ru/seller/shunana/"',
                              '<a href="https://www.ozon.ru/seller/shunana/"')
    r = ozon.parse_pdp_html(html, anchor_ids={SKU})
    assert r.seller_name is None
    assert r.seller_status is SellerStatus.UNKNOWN_LAYOUT
    # Название при этом сохраняется: половина результата лучше нуля.
    assert r.name is not None
    assert r.verdict is Verdict.PARTIAL


def test_missing_seller_block_never_invents_one() -> None:
    """Замер: один из трёх рендеров пришёл коротким и без блока магазина.

    Правильный исход — PARTIAL с одним названием, а не догадка о продавце.
    """
    html = _fixture().replace("/seller/shunana/", "/nothing/")
    r = ozon.parse_pdp_html(html, anchor_ids={SKU})
    assert r.name is not None
    assert r.seller_name is None
    assert r.verdict is Verdict.PARTIAL
    assert not r.complete


def test_composer_parser_would_have_failed_on_this_page() -> None:
    """Обоснование существования HTML-лейна.

    Разбор под composer-api на отрендеренной странице возвращал
    ``silent_empty`` — то есть сообщал о блоке там, где данные есть, и
    штрафовал исправный адрес. ``widgetStates`` в этой странице ноль.
    """
    html = _fixture()
    assert html.count("widgetStates") == 0
    assert ozon.classify_response(html, None, 200) is Verdict.SILENT_EMPTY
    assert Verdict.SILENT_EMPTY in CHARGES_PROXY
    # А HTML-разбор той же страницы отдаёт и название, и продавца.
    assert ozon.parse_pdp_html(html, anchor_ids={SKU}).complete
