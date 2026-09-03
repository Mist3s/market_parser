"""Реестр, валидация, SSRF и каноникализация.

Здесь же живут контрактные тесты из спецификации: порядок проверок пути,
дизъюнктность pdp и shortlink, и допуск короткой ссылки в раскрутку — то
самое требование, которое первая редакция дизайна тихо теряла.
"""

from __future__ import annotations

import ipaddress

import pytest

from mktlink.urls.canonical import canonicalise
from mktlink.urls.registry import (
    ALLOWED_HOSTS,
    REGISTRY,
    NotAProductUrl,
    UnknownHost,
    match_path,
    unwind_eligible,
)
from mktlink.urls.ssrf import (
    SsrfRejected,
    UnwindChallenged,
    check_path_veto,
    check_resolved,
    is_blocked_ip,
    looks_like_challenge_host,
)
from mktlink.urls.validate import UrlRejected, validate

# --- реестр ------------------------------------------------------------------


def test_scope_is_exactly_three_marketplaces() -> None:
    assert set(REGISTRY) == {"ozon", "wb", "ym"}
    assert "detmir" not in ALLOWED_HOSTS and "samokat.ru" not in ALLOWED_HOSTS


def test_no_wildcard_subdomains_anywhere() -> None:
    """На отсутствии wildcard стоит весь DNS-аргумент политики SSRF."""
    for host in ALLOWED_HOSTS:
        assert not host.startswith("."), host
        assert "*" not in host, host


@pytest.mark.parametrize(
    ("host", "path", "mp", "ids"),
    [
        ("www.ozon.ru", "/product/smes-nutrilon-1234567890/", "ozon", {"sku": "1234567890"}),
        ("ozon.ru", "/product/1234567890", "ozon", {"sku": "1234567890"}),
        ("www.wildberries.ru", "/catalog/12345678/detail.aspx", "wb", {"nm": "12345678"}),
        ("market.yandex.ru", "/card/pyure-semper/4382957723", "ym", {"sku_id": "4382957723"}),
        ("market.yandex.ru", "/product--smes/1159015329", "ym", {"product_id": "1159015329"}),
        ("market.yandex.ru", "/product/1159015329", "ym", {"product_id": "1159015329"}),
    ],
)
def test_pdp_forms_are_recognised(host: str, path: str, mp: str, ids: dict) -> None:
    m = match_path(host, path)
    assert m.marketplace == mp
    assert m.is_pdp
    for k, v in ids.items():
        assert m.ids[k] == v


def test_wb_product_link_survives_the_not_pdp_overlap() -> None:
    """Порядок проверок несущий: pdp раньше not_pdp.

    ``/catalog/12345678/detail.aspx`` матчит и pdp, и not_pdp (``12345678``
    подходит под ``[a-z0-9-]+``). При обратном порядке отвергалась бы каждая
    ссылка на товар Wildberries.
    """
    wb = REGISTRY["wb"]
    path = "/catalog/12345678/detail.aspx"
    assert any(p.match(path) for p in wb.pdp)
    assert any(p.match(path) for p in wb.not_pdp), "перекрытие реально, иначе тест бессмыслен"
    assert match_path("www.wildberries.ru", path).is_pdp


def test_short_link_from_the_mobile_app_reaches_unwinding() -> None:
    """Требование заказчика: короткая ссылка из приложения обязана работать."""
    m = match_path("ozon.ru", "/t/AbC123")
    assert m.is_shortlink and not m.is_pdp
    assert unwind_eligible("ozon.ru", "/t/AbC123")

    m = match_path("market.yandex.ru", "/cc/AbC1_2-3")
    assert m.is_shortlink
    assert unwind_eligible("market.yandex.ru", "/cc/AbC1_2-3")


def test_universal_shorteners_are_never_admitted() -> None:
    """У них нет владеющей строки реестра, значит нет заключения в маркетплейс."""
    for host in ("bit.ly", "clck.ru", "ya.cc", "t.co"):
        assert not unwind_eligible(host, "/abc123")
        with pytest.raises(UnknownHost):
            match_path(host, "/abc123")


def test_wb_admits_nothing_to_unwinding() -> None:
    """Форма шеринга WB не измерена, поэтому пустой кортеж — «ничего», а не «всё»."""
    assert REGISTRY["wb"].shortlink == ()
    assert not unwind_eligible("www.wildberries.ru", "/t/abc123")


def test_pdp_and_shortlink_are_disjoint_over_every_registry_form() -> None:
    samples = [
        "/product/smes-1234567890/",
        "/product/1234567890",
        "/t/AbC123",
        "/catalog/12345678/detail.aspx",
        "/card/slug/4382957723",
        "/card/4382957723",
        "/product--slug/1159015329",
        "/product/1159015329",
        "/offer/abcdefghij0123456789AB",
        "/cc/AbC1_2-3",
    ]
    for rule in REGISTRY.values():
        for path in samples:
            hit_pdp = any(p.match(path) for p in rule.pdp)
            hit_short = any(p.match(path) for p in rule.shortlink)
            assert not (hit_pdp and hit_short), f"{rule.marketplace}: {path}"


def test_showcaptcha_matches_nothing_and_is_an_unknown_path() -> None:
    with pytest.raises(NotAProductUrl):
        match_path("market.yandex.ru", "/showcaptcha?cc=1")


def test_category_pages_are_classified_for_the_error_text() -> None:
    with pytest.raises(NotAProductUrl) as ei:
        match_path("www.ozon.ru", "/category/detskoe-pitanie-7030/")
    assert ei.value.classified is not None


# --- валидация ---------------------------------------------------------------


def test_valid_url_passes() -> None:
    p = validate("https://www.ozon.ru/product/smes-1234567890/?utm_source=x")
    assert p.host == "www.ozon.ru"
    assert p.path == "/product/smes-1234567890/"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-a-url",
        "http://www.ozon.ru/product/1234567890",  # https обязателен
        "ftp://www.ozon.ru/x",
        "https://user:pass@www.ozon.ru/x",  # userinfo
        "https://www.ozon.ru:8080/x",  # нестандартный порт
        "https://127.0.0.1/x",  # IP-литерал
        "https://[::1]/x",
        "https://www.ozon.ru/x\nHost: evil",  # управляющие символы
        " https://www.ozon.ru/x",  # пробел
    ],
)
def test_malformed_urls_are_rejected(bad: str) -> None:
    with pytest.raises(UrlRejected):
        validate(bad)


def test_oversized_url_is_rejected_before_parsing() -> None:
    with pytest.raises(UrlRejected):
        validate("https://www.ozon.ru/product/" + "a" * 3000)


def test_homoglyph_host_is_caught_by_mixed_script_not_by_idna() -> None:
    """IDNA пропускает смешение скриптов; ловит его отдельная проверка."""
    cyrillic_o = "о"
    with pytest.raises(UrlRejected) as ei:
        validate(f"https://{cyrillic_o}zon.ru/product/1234567890")
    assert "mixed-script" in str(ei.value)


def test_scheme_downgrade_is_refused_not_upgraded() -> None:
    with pytest.raises(UrlRejected):
        validate("http://www.ozon.ru/product/1234567890")


# --- SSRF --------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # метаданные облака
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fec0::1",  # site-local: deprecated, но маршрутизируем
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:10.0.0.1",
    ],
)
def test_private_and_special_addresses_are_blocked(addr: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(addr)), addr


@pytest.mark.parametrize("addr", ["93.158.134.3", "2a02:6b8::2:242"])
def test_public_addresses_pass(addr: str) -> None:
    assert not is_blocked_ip(ipaddress.ip_address(addr))


def test_every_resolved_address_must_pass_not_just_the_first() -> None:
    """DNS с одним публичным и одним приватным адресом — классическая дыра."""
    with pytest.raises(SsrfRejected):
        check_resolved(["93.158.134.3", "10.0.0.1"])
    check_resolved(["93.158.134.3", "93.158.134.4"])


def test_empty_dns_answer_is_a_rejection_not_a_pass() -> None:
    with pytest.raises(SsrfRejected):
        check_resolved([])


def test_yandex_challenge_on_the_same_host_is_a_terminal_not_a_hop() -> None:
    """Дыра, видимая только на Яндексе: челлендж приходит на market.yandex.ru."""
    with pytest.raises(UnwindChallenged) as ei:
        check_path_veto(
            "ym",
            "/showcaptcha",
            egress="direct",
            url="https://market.yandex.ru/showcaptcha?cc=1",
        )
    assert ei.value.egress == "direct"


def test_path_veto_leaves_product_paths_alone() -> None:
    check_path_veto("ym", "/card/slug/4382957723", egress="direct", url="https://x/y")
    check_path_veto("ozon", "/product/1234567890", egress="direct", url="https://x/y")


def test_offsite_challenge_host_is_recognised_as_a_challenge() -> None:
    """Иначе блок маркетплейса рапортуется клиенту как «твоя ссылка плохая»."""
    assert looks_like_challenge_host("yandex.ru")
    assert looks_like_challenge_host("captcha.yandex.net")
    assert not looks_like_challenge_host("market.yandex.ru.evil.com")
    assert not looks_like_challenge_host("www.ozon.ru")


# --- каноникализация ---------------------------------------------------------


def test_noise_is_dropped_and_offer_choice_is_kept() -> None:
    m = match_path("www.ozon.ru", "/product/smes-1234567890/")
    c = canonicalise(
        "www.ozon.ru",
        "/product/smes-1234567890/",
        "utm_source=mail&sku=99887766&yclid=1&cpc=zzz",
        m,
    )
    assert c.offer == (("sku", "99887766"),)
    assert "utm_source" not in c.url and "yclid" not in c.url
    assert "sku=99887766" in c.url


def test_dropping_the_offer_param_would_change_the_seller_so_it_changes_the_key() -> None:
    """Два оффера одной карточки — два ключа кэша и два разных продавца."""
    m = match_path("market.yandex.ru", "/product--slug/1159015329")
    a = canonicalise("market.yandex.ru", "/product--slug/1159015329", "sku=1", m)
    b = canonicalise("market.yandex.ru", "/product--slug/1159015329", "sku=2", m)
    none = canonicalise("market.yandex.ru", "/product--slug/1159015329", "", m)
    assert a.cache_key != b.cache_key != none.cache_key
    assert none.cache_key.endswith("@*")


def test_id_namespaces_are_not_conflated() -> None:
    """У Я.Маркета product_id и sku_id — разные вещи для одной карточки."""
    by_sku = canonicalise(
        "market.yandex.ru",
        "/card/slug/4382957723",
        "",
        match_path("market.yandex.ru", "/card/slug/4382957723"),
    )
    by_pid = canonicalise(
        "market.yandex.ru",
        "/product--slug/1159015329",
        "",
        match_path("market.yandex.ru", "/product--slug/1159015329"),
    )
    assert ":s4382957723" in by_sku.cache_key
    assert ":p1159015329" in by_pid.cache_key


def test_www_and_bare_host_share_one_cache_key() -> None:
    a = canonicalise(
        "ozon.ru", "/product/1234567890", "", match_path("ozon.ru", "/product/1234567890")
    )
    b = canonicalise(
        "www.ozon.ru", "/product/1234567890", "", match_path("www.ozon.ru", "/product/1234567890")
    )
    assert a.cache_key == b.cache_key
    assert a.url == b.url == "https://www.ozon.ru/product/1234567890"


def test_unknown_query_param_is_treated_as_significant_only_if_it_selects_an_offer() -> None:
    """Асимметрия намеренная: неизвестный параметр не попадает в ключ, если он
    не объявлен параметром выбора оффера, но и не ломает канонический URL."""
    m = match_path("www.wildberries.ru", "/catalog/12345678/detail.aspx")
    c = canonicalise("www.wildberries.ru", "/catalog/12345678/detail.aspx", "size=42", m)
    assert c.offer == ()
    assert c.cache_key.endswith("@*")
