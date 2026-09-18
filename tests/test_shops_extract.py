"""Обычные магазины: один экстрактор на четырнадцать разметок и реестр по строке.

Фикстуры — обрезанные живые карточки (снято 2026-09-18): ``<head>`` целиком,
JSON-LD, окрестности ``<h1>`` и область ``schema.org/Product``. Добавление
магазина в реестр без фикстуры здесь падает намеренно.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from mktlink.shops.extract import (
    acceptable,
    clean_title,
    extract_name,
    looks_like_not_found,
    site_name,
)
from mktlink.shops.registry import SHOPS, lookup

FIXTURES = Path(__file__).parent / "fixtures" / "shops"

#: (фикстура, хост, название). Название — ровно то, что стоит в ``<h1>``
#: карточки после нормализации пробелов.
CARDS = [
    (
        "moschaitorg.ru",
        "moschaitorg.ru",
        "Белый чай - Бай Хао Инь Чжэнь (Серебряные иглы с белыми волосками)",
    ),
    ("moschaitorg.ru-kenya", "moschaitorg.ru", "Чай черный кенийский OPA"),
    ("realchinatea.ru", "realchinatea.ru", "Чай Габа Улун"),
    ("moychay.ru", "moychay.ru", "Шен пуэр Мэнхай Лао, 2012"),
    ("artoftea.ru", "artoftea.ru", "Улун Габа Али Шань, премиум"),
    ("teaworkshop.ru", "teaworkshop.ru", "Пуэр Шу «Бодрейший No 2» 2023 г."),
    (
        "imperatormin.ru",
        "imperatormin.ru",
        "Шен пуэр россыпь с гор Баошань, (граница с Бирмой) 2019г",
    ),
    (
        "chaline.ru",
        "chaline.ru",
        'Шэн пуэр со старых деревьев горы "Хуачжу лянцзы" марки "Чайная Линия" 200 г Весна 2019',
    ),
    ("teaboom.ru", "teaboom.ru", 'Чай зелёный ароматизированный "Бенгальский Тигр"'),
    ("aromatchaya.ru", "aromatchaya.ru", "“Гун Тин Гун Бин” Шу Пуэр 357 гр. 2018г."),
    (
        "chayniy-put.ru",
        "chayniy-put.ru",
        "Шу Пуэр Кремовый Тигр – мягкий и насыщенный (100 г)",
    ),
    (
        "kofcheg.ru",
        "kofcheg.ru",
        "Улун Да Хун Пао No 1 (Большой красный халат)ПРЕМИУМ Китайский зеленый чай",
    ),
    ("teaguru.ru", "teaguru.ru", "Да Хун Пао Нун Сян"),
    (
        "tiptoptea.ru",
        "tiptoptea.ru",
        "Красный копченый чай Чжен Шан Сяо Чжун (Лапсанг Сушонг)",
    ),
    ("chaekshop.ru", "chaekshop.ru", "Набор «Чилл и Затерянные земли», ЧАЁК"),
]

#: Ссылки, которые присылал заказчик. Все — карточки.
PRODUCT_URLS = [
    "https://realchinatea.ru/shop/tayvanskiy-ulun-gaba",
    "https://moschaitorg.ru/product/baj-khao-in-chzhen-belye-igly/",
    "https://artoftea.ru/oolong/gaba-oolong/gaba-alishan",
    "https://teaworkshop.ru/product/puer-bodreishiy-2-2023-g",
    "https://moychay.ru/catalog/puer/shen_puer_pressovannyj/menhay-lao-2012",
    "https://imperatormin.ru/product/shen-puer-rossyip-s-gor-baoshan",
    "https://chayniy-put.ru/tproduct/625702138902-shu-puer-kremovii-tigr-myagkii-i-nasisch",
    "https://chaekshop.ru/product/nabor_chill_i_zateryannye_zemli_",
    "https://aromatchaya.ru/product/gun-tin-gun-bin-shu-puer-2018-g-2018/",
    "https://tiptoptea.ru/catalog/red-tea/chzhen-shan-syao-chzhun/",
    "https://teaguru.ru/product/da-khun-pao-nun-syan/",
    "https://teaboom.ru/product/chaj-zelyonyj-aromatizirovannyj-bengalskij-tigr",
    "https://moschaitorg.ru/product/chay-chernyy-keniyskiy-opa712/",
    "https://kofcheg.ru/chay/ulun-oolong/kitayskiy-zelenyy-chay-ulun-da-hun-pao-bolshoy-krasnyy-halat-premium",
    "https://chaline.ru/catalog/chay/puer_i_chyernyy_chay/pressovannyy_shen_puer/221344/?oid=221352",
]

#: Категории, поиск, главная — отвергаются по форме пути, до сети.
NOT_PRODUCT_URLS = [
    "https://moychay.ru/",
    "https://moychay.ru/catalog/puer",
    "https://artoftea.ru/oolong/gaba-oolong",
    "https://kofcheg.ru/chay/ulun-oolong",
    "https://moschaitorg.ru/catalog/",
    "https://tiptoptea.ru/catalog/red-tea/",
    "https://chaline.ru/catalog/chay/puer_i_chyernyy_chay/",
    "https://realchinatea.ru/shop",
    "https://chaekshop.ru/catalog/puer",
    "https://teaguru.ru/category/oolong/",
]


def page(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# --- экстрактор на живых карточках -----------------------------------------------


@pytest.mark.parametrize(("fixture", "host", "expected"), CARDS)
def test_name_is_read_from_every_known_shop(fixture: str, host: str, expected: str) -> None:
    shop = lookup(host)
    assert shop is not None
    found = extract_name(page(fixture), shop.name_sources)
    assert found.name == expected
    # ЗАМЕР 2026-09-18: на всех карточках первый источник лестницы — h1.
    # Если это перестанет быть так, тест скажет, у какого магазина уехала
    # разметка, а сервис продолжит работать со следующей ступени.
    assert found.source == "h1"


def test_every_shop_in_the_registry_has_a_fixture() -> None:
    files = [p.name for p in FIXTURES.glob("*.html")]
    missing = [s.host for s in SHOPS if not any(f.startswith(s.host) for f in files)]
    assert missing == [], f"добавьте фикстуру в tests/fixtures/shops для {missing}"


def test_microdata_misfire_is_skipped_by_the_stoplist() -> None:
    """У kofcheg первый itemprop=name в области Product — «Главная» из крошек."""
    found = extract_name(page("kofcheg.ru"), ("microdata", "h1"))
    assert found.source == "h1"
    assert dict(found.candidates)["microdata"] == "Главная"


def test_site_name_comes_from_og_site_name_only() -> None:
    assert site_name(page("artoftea.ru")) == "Art of Tea. Искусство Чая"
    assert site_name(page("moychay.ru")) is None


# --- отдельные источники ------------------------------------------------------------


def test_several_h1_pick_the_one_closest_to_the_title() -> None:
    html = (
        "<html><head><title>Те Гуань Инь купить | Лавка</title></head>"
        "<body><h1>Лавка</h1><h1>Те Гуань Инь</h1></body></html>"
    )
    assert extract_name(html).name == "Те Гуань Инь"


def test_ldjson_product_is_found_inside_graph() -> None:
    html = (
        '<html><head><script type="application/ld+json">{"@context":"https://schema.org",'
        '"@graph":[{"@type":"WebPage","name":"x"},{"@type":"Product","name":"Дянь Хун"}]}'
        "</script></head><body></body></html>"
    )
    found = extract_name(html)
    assert (found.source, found.name) == ("ldjson", "Дянь Хун")


def test_nothing_usable_gives_no_name_but_keeps_candidates() -> None:
    html = "<html><head><title>404</title></head><body><h1>Главная</h1></body></html>"
    found = extract_name(html)
    assert found.name is None
    assert dict(found.candidates) == {"h1": "Главная", "title": "404"}


def test_unknown_source_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown name source"):
        extract_name("<h1>x</h1>", ("xpath",))


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        (
            "Улун Габа Али Шань, премиум — 2 280 ₽, купить | Art of Tea",
            "Улун Габа Али Шань, премиум",
        ),
        ("Чай Габа Улун - купить в Москве по выгодной цене", "Чай Габа Улун"),
        ("Китайский чай: купить Белый чай - Бай Хао Инь Чжэнь", "Белый чай - Бай Хао Инь Чжэнь"),
        ("Да Хун Пао Нун Сян купить в Москве", "Да Хун Пао Нун Сян"),
        (
            "Купить Шен пуэр Мэнхай Лао 2012 в интернет-магазине Мойчай.ру",
            "Шен пуэр Мэнхай Лао 2012",
        ),
        ("Шу Пуэр Кремовый Тигр | Чайный Путь", "Шу Пуэр Кремовый Тигр"),
        ("", None),
        (None, None),
    ],
)
def test_clean_title_strips_seo_noise(raw: str | None, cleaned: str | None) -> None:
    assert clean_title(raw) == cleaned


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("Те Гуань Инь", True),
        ("Главная", False),
        ("Товар не найден", False),
        ("12345", False),
        ("Те", False),
        ("x" * 301, False),
    ],
)
def test_acceptable(name: str, ok: bool) -> None:
    assert acceptable(name) is ok


def test_soft_404_is_recognised_in_title_or_h1() -> None:
    assert looks_like_not_found("<title>Страница не найдена</title><h1>Ой</h1>")
    assert looks_like_not_found("<title>Чай</title><h1>Товар не найден</h1>")
    assert not looks_like_not_found("<title>Чай</title><h1>Те Гуань Инь</h1>")


# --- реестр -------------------------------------------------------------------------


def test_lookup_ignores_www_and_case() -> None:
    assert lookup("moychay.ru") is lookup("www.moychay.ru") is lookup("MoyChay.RU")
    assert lookup("example.com") is None


@pytest.mark.parametrize("url", PRODUCT_URLS)
def test_customer_links_are_product_paths(url: str) -> None:
    parts = urlsplit(url)
    shop = lookup(parts.hostname or "")
    assert shop is not None
    assert shop.is_product_path(parts.path)


@pytest.mark.parametrize("url", NOT_PRODUCT_URLS)
def test_categories_are_rejected_by_path_shape(url: str) -> None:
    parts = urlsplit(url)
    shop = lookup(parts.hostname or "")
    assert shop is not None
    assert not shop.is_product_path(parts.path)


def test_moychay_category_that_looks_like_a_product_is_caught_by_the_marker() -> None:
    """Трёхсегментная категория проходит фильтр пути; её отсекает маркер.

    ЗАМЕР 2026-09-18: категория /catalog/puer/shen_puer_pressovannyj отдаёт
    200 с h1 «Шэн пуэр прессованный» и без единого schema.org/Product.
    """
    shop = lookup("moychay.ru")
    assert shop is not None
    assert shop.is_product_path("/catalog/puer/shen_puer_pressovannyj")
    assert shop.product_marker is not None
    assert shop.product_marker.search(page("moychay.ru")) is not None
    assert shop.product_marker.search("<h1>Шэн пуэр прессованный</h1>") is None


def test_chaline_keeps_the_variant_parameter_and_drops_the_rest() -> None:
    shop = lookup("chaline.ru")
    assert shop is not None
    got = shop.canonical("/catalog/chay/x/221344/", "utm_source=tg&oid=221352&yclid=1")
    assert got == "https://chaline.ru/catalog/chay/x/221344/?oid=221352"
    assert lookup("moychay.ru").canonical("/catalog/a/b/c", "utm_source=tg") == (
        "https://moychay.ru/catalog/a/b/c"
    )


def test_spa_shop_is_fetched_through_the_prerender_parameter() -> None:
    shop = lookup("teaworkshop.ru")
    assert shop is not None
    canonical = "https://teaworkshop.ru/product/puer-bodreishiy-2-2023-g"
    assert shop.fetch_url(canonical) == canonical + "?_escaped_fragment_="
    assert shop.fetch_url(canonical + "?v=1") == canonical + "?v=1&_escaped_fragment_="
    assert lookup("moychay.ru").fetch_url("https://moychay.ru/x") == "https://moychay.ru/x"
