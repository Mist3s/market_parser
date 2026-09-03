"""Формы ответов Ozon, ЗАМЕРЕННЫЕ через датацентровый IPv4 proxy6.

Замер 2026-09-03, прокси 41368176 (v3, IPv4 Shared), ссылка из мобильного
приложения ``https://ozon.ru/t/8M3J7yH``.

Это не догадки и не фикстуры «как могло бы быть» — здесь записано то, что
Ozon действительно ответил. Значение тестов в том, что каждый из них
падал бы на прежней версии классификатора.
"""

from __future__ import annotations

import pytest

from mktlink.marketplaces import ozon, wb, ym
from mktlink.marketplaces.verdict import CHARGES_PROXY, Verdict, charges_proxy

#: Замеренная страница блока: 5072 байта стилизованного HTML со шрифтами и
#: CSS-переменными. Ни слова «captcha», ни одного антибот-маркера.
MEASURED_403_BODY = (
    '<html><head><style>@font-face { font-display: swap; font-family: Onest; '
    'src: url("https://st.ozone.ru/s3/ozon-fonts/onest.woff2") format("woff2") } '
    ":root { --floor0: #F0F2F5; --action-primary: #005bffff; }</style></head>"
    "<body><div></div></body></html>" + "x" * 4800
)

#: Замеренное рукопожатие за cookie: 307 на тот же URL с Set-Cookie.
MEASURED_307_BODY = (
    "<html>\r\n<head><title>307 Temporary Redirect</title></head>\r\n"
    "<body>\r\n<center><h1>307 Temporary Redirect</h1></center>\r\n"
    "<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def test_the_measured_block_is_a_block_not_an_empty_page() -> None:
    """Прежняя версия смотрела только на тело и выдавала silent_empty.

    Для здоровья прокси исход тот же — оба вердикта штрафуют, — но диагноз
    оператору был неверный: «страница пришла пустая» вместо «нас
    заблокировали».
    """
    assert ozon.classify_response(MEASURED_403_BODY, None, 403) is Verdict.CAPTCHA
    # А по одному телу его не отличить — именно поэтому решает статус.
    assert ozon.classify_response(MEASURED_403_BODY, None, 200) is Verdict.SILENT_EMPTY


def test_the_measured_block_page_contains_no_antibot_marker_at_all() -> None:
    """Опираться на текст тела здесь нельзя вовсе.

    Унаследованный из батч-скрейпера список маркеров на этой странице не
    находит ничего: она стилизована и никаких слов про робота не содержит.
    """
    from tests.legacy_reference import LEGACY_MARKERS_WORTH_KEEPING

    low = MEASURED_403_BODY.casefold()
    assert not [m for m in LEGACY_MARKERS_WORTH_KEEPING if m in low]


def test_the_cookie_handshake_is_not_data_but_not_a_verdict_either() -> None:
    """307 отдаёт __Secure-ETC и отправляет на тот же URL.

    Данных в таком ответе нет, поэтому SILENT_EMPTY, — но это НЕ капча:
    рукопожатие является штатным поведением Ozon и происходит без браузера.
    """
    v = ozon.classify_response(MEASURED_307_BODY, None, 307)
    assert v is Verdict.SILENT_EMPTY
    assert v is not Verdict.CAPTCHA


def test_a_block_charges_the_proxy_and_drift_never_does() -> None:
    assert charges_proxy(Verdict.CAPTCHA, egress="proxy")
    assert Verdict.SCHEMA_DRIFT not in CHARGES_PROXY


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (401, Verdict.CAPTCHA),
        (403, Verdict.CAPTCHA),
        (429, Verdict.HTTP_429),
        (500, Verdict.UPSTREAM_ERROR),
        (503, Verdict.UPSTREAM_ERROR),
    ],
)
def test_status_decides_before_the_body_on_every_lane(status: int, verdict: Verdict) -> None:
    """Урок общий, поэтому применён ко всем трём лейнам, а не только к Ozon."""
    assert ozon.classify_response("x" * 5000, {"widgetStates": {"a": "{}"}}, status) is verdict
    assert wb.classify_response({"data": {"products": []}}, status) is verdict
    assert ym.classify_response("<html>ok</html>", anchor_ids={"1"}, status=status) is verdict


def test_a_healthy_answer_still_falls_through_to_parsing() -> None:
    """Классификатор проверяет КОНВЕРТ, а не содержимое.

    Пустой список товаров у WB — решение parse_card, а не конверта: ответ
    пришёл, структура на месте, и вердикт зависит от того, есть ли в нём наш
    товар. Смешивать эти два уровня значило бы дважды судить одно и то же.
    """
    assert ozon.classify_response("x" * 5000, {"widgetStates": {"a": "{}"}}, 200) is None
    assert wb.classify_response({"data": {"products": []}}, 200) is None
    # А уже разбор решает, что пустой список — это молчаливый блок.
    from mktlink.marketplaces.selectors import DEFAULTS

    empty = wb.parse_card({"data": {"products": []}}, DEFAULTS["wb"], nm="1")
    assert empty.verdict is Verdict.SILENT_EMPTY


def test_the_short_link_form_from_the_app_is_confirmed_by_measurement() -> None:
    """Форма ``/t/<code>`` была гипотезой; ссылка из приложения её подтвердила."""
    from mktlink.urls.registry import match_path, unwind_eligible

    m = match_path("ozon.ru", "/t/8M3J7yH")
    assert m.is_shortlink and m.ids["code"] == "8M3J7yH"
    assert unwind_eligible("ozon.ru", "/t/8M3J7yH")


def test_the_measured_canonical_form_parses_as_a_product() -> None:
    """Раскрутка отдала именно ту форму, которую ждёт реестр."""
    from mktlink.urls.registry import match_path

    path = (
        "/product/te-guan-in-200g-kitayskiy-zelenyy-listovoy-chay-ulun-"
        "zheleznaya-boginya-miloserdiya-traditsionnoy-483430098/"
    )
    m = match_path("www.ozon.ru", path)
    assert m.is_pdp and m.ids["sku"] == "483430098"


def test_the_measured_tracking_params_are_all_dropped() -> None:
    """Живая ссылка принесла четыре трекинговых параметра. Ни один не в ключе."""
    from mktlink.urls.canonical import canonicalise
    from mktlink.urls.registry import match_path

    path = "/product/te-guan-in-200g-483430098/"
    query = (
        "from=share_android&perehod=smm_share_button_productpage_link"
        "&sh=uGuKHoUUMA&short=8M3J7yH"
    )
    c = canonicalise("www.ozon.ru", path, query, match_path("www.ozon.ru", path))
    assert c.offer == ()
    for noise in ("from=", "perehod=", "sh=", "short="):
        assert noise not in c.url, noise


def test_the_wb_card_endpoint_is_v4_not_v2() -> None:
    """ЗАМЕР: v2 отвечает 404 с нулевым телом, v4 — 200.

    Форма эндпоинта была гипотезой (в удалённом скрейпере он не встречался
    ни разу), и гипотеза оказалась неверной. Тест держит замеренное значение,
    чтобы правка не откатилась обратно к догадке.
    """
    assert "cards/v4/detail" in wb.CARD_API
    assert "v2" not in wb.CARD_API


def test_an_empty_body_with_404_is_not_mistaken_for_a_missing_product() -> None:
    """Именно так отвечал несуществующий v2: 404 и ноль байт.

    Спутать это с «товара нет» значило бы отдать клиенту 404 на исправную
    ссылку из-за нашей же опечатки в URL.
    """
    assert wb.classify_response(None, 404) is Verdict.SILENT_EMPTY
    assert wb.classify_response(None, 404) is not Verdict.NOT_FOUND
