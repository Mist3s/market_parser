"""Путь запроса ``/v1/shop`` на скриптованном транспорте: без сети и без сна."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mktlink.api.schemas import ShopRequest
from mktlink.constants import RETRY_AFTER_FETCH_S, RETRY_AFTER_MINT_S
from mktlink.shops.fetch import ShopFetcher, ShopUnreachable
from mktlink.shops.service import ShopDeps, handle_shop
from mktlink.store.cache import ProductCache
from mktlink.timing.deadline import DeadlineExceeded

FIXTURES = Path(__file__).parent / "fixtures" / "shops"
MOYCHAY = "https://moychay.ru/catalog/puer/shen_puer_pressovannyj/menhay-lao-2012"
MOYCHAY_CATEGORY = "https://moychay.ru/catalog/puer/shen_puer_pressovannyj"
REALCHINATEA = "https://realchinatea.ru/shop/tayvanskiy-ulun-gaba"
PAGE = (FIXTURES / "moychay.ru.html").read_text(encoding="utf-8")
PROXY = "http://user:pw@proxy.test:8080"


class Transport:
    """Очередь ответов и журнал вызовов. Последний ответ повторяется."""

    def __init__(self, *responses: tuple[int, str, str] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, url: str, *, headers: dict[str, str], proxy: str | None, timeout_ms: int
    ) -> tuple[int, str, str]:
        self.calls.append({"url": url, "proxy": proxy, "timeout_ms": timeout_ms})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class AgedCache:
    """Кэш с управляемым возрастом записи — для сценариев устаревания."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.age = 0

    def get(self, key: str, *, fresh_ttl_s: int | None = None) -> dict[str, Any] | None:
        row = self.rows.get(key)
        if row is None or (fresh_ttl_s is not None and self.age > fresh_ttl_s):
            return None
        return row

    def get_stale(self, key: str, max_age_s: int) -> tuple[dict[str, Any], int] | None:
        row = self.rows.get(key)
        if row is None or self.age > max_age_s:
            return None
        return row, self.age

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.rows[key] = value


def deps(transport: Transport, *, proxy: str | None = None, **kw: Any) -> ShopDeps:
    max_bytes = kw.pop("max_bytes", 3 << 20)
    fetcher = ShopFetcher(transport, proxy_for=lambda: proxy, max_bytes=max_bytes)
    return ShopDeps(cache=kw.pop("cache", ProductCache()), fetcher=fetcher, **kw)


async def ask(url: str, d: ShopDeps, **fields: Any):
    return await handle_shop(ShopRequest(url=url, **fields), d)


# --- счастливый путь и кэш --------------------------------------------------------------


async def test_one_direct_request_gives_name_and_shop() -> None:
    t = Transport((200, PAGE, MOYCHAY))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status) == (200, "ok")
    assert r.product.name == "Шен пуэр Мэнхай Лао, 2012"
    assert r.product.source == "h1"
    assert (r.shop.host, r.shop.name, r.shop.platform) == ("moychay.ru", "Мойчай.ру", "inertia")
    assert r.url.canonical == MOYCHAY
    assert (r.meta.cache, r.meta.egress) == ("miss", "direct")
    assert [name for name, _ in r.meta.ledger] == ["shop.fetch"]
    assert t.calls == [{"url": MOYCHAY, "proxy": None, "timeout_ms": t.calls[0]["timeout_ms"]}]
    assert t.calls[0]["timeout_ms"] > 5000


async def test_second_request_is_a_cache_hit_and_www_shares_the_key() -> None:
    t = Transport((200, PAGE, MOYCHAY))
    d = deps(t)
    await ask(MOYCHAY, d)
    code, r = await ask(MOYCHAY.replace("https://", "https://www."), d)
    assert (code, r.status, r.meta.cache) == (200, "ok", "hit")
    assert r.product.name == "Шен пуэр Мэнхай Лао, 2012"
    assert r.meta.detail["age_s"] >= 0
    assert len(t.calls) == 1


async def test_force_refresh_bypasses_the_cache() -> None:
    t = Transport((200, PAGE, MOYCHAY))
    d = deps(t)
    await ask(MOYCHAY, d)
    code, r = await ask(MOYCHAY, d, force_refresh=True)
    assert (r.status, r.meta.cache) == ("ok", "miss")
    assert len(t.calls) == 2


async def test_stale_is_served_when_the_shop_is_down_and_the_client_allows_it() -> None:
    cache = AgedCache()
    t = Transport((200, PAGE, MOYCHAY), ShopUnreachable("connect_timeout"))
    d = deps(t, cache=cache, ttl_s=60)
    await ask(MOYCHAY, d)
    cache.age = 3600
    code, r = await ask(MOYCHAY, d)
    assert (code, r.status) == (200, "stale")
    assert r.product.name == "Шен пуэр Мэнхай Лао, 2012"
    assert (r.meta.degraded, r.meta.reason, r.meta.cache) == (True, "shop_unreachable", "stale")
    assert r.meta.detail == {"age_s": 3600}


async def test_stale_is_not_served_when_the_client_forbids_it() -> None:
    cache = AgedCache()
    t = Transport((200, PAGE, MOYCHAY), ShopUnreachable("connect_timeout"))
    d = deps(t, cache=cache, ttl_s=60)
    await ask(MOYCHAY, d)
    cache.age = 3600
    code, r = await ask(MOYCHAY, d, max_stale_s=0)
    assert (code, r.status, r.meta.reason) == (202, "pending", "shop_unreachable")
    assert r.meta.retry_after_seconds == RETRY_AFTER_MINT_S
    assert r.meta.detail == {"error": "connect_timeout"}


# --- отказы без сети ----------------------------------------------------------------------


async def test_unknown_host_is_rejected_before_any_socket() -> None:
    t = Transport((200, PAGE, MOYCHAY))
    code, r = await ask("https://tea.example/product/x", deps(t))
    assert (code, r.status) == (422, "shop_not_supported")
    assert r.meta.detail == {"host": "tea.example"}
    assert t.calls == []


async def test_category_url_is_rejected_by_path_shape() -> None:
    t = Transport((200, PAGE, MOYCHAY))
    code, r = await ask("https://moychay.ru/catalog/puer", deps(t))
    assert (code, r.status) == (422, "not_a_product_url")
    assert r.shop.name == "Мойчай.ру"
    assert t.calls == []


async def test_http_url_is_invalid() -> None:
    code, r = await ask(MOYCHAY.replace("https", "http"), deps(Transport((200, PAGE, MOYCHAY))))
    assert (code, r.status) == (422, "invalid_url")


async def test_budget_below_the_floor_is_rejected() -> None:
    code, r = await ask(MOYCHAY, deps(Transport((200, PAGE, MOYCHAY)), budget_ms=1000))
    assert (code, r.status) == (422, "invalid_budget")


# --- незнакомые хосты по явному разрешению ------------------------------------------------


GENERIC = (
    "<html><head><title>Те Гуань Инь купить</title>"
    '<meta property="og:site_name" content="Лавка"></head>'
    "<body><h1>Те Гуань Инь</h1></body></html>"
)


async def test_unknown_host_is_read_generically_when_allowed_and_public() -> None:
    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    t = Transport((200, GENERIC, "https://tea.example/goods/tgy"))
    code, r = await ask(
        "https://tea.example/goods/tgy", deps(t, allow_unknown_hosts=True, resolve_dns=public)
    )
    assert (code, r.status) == (200, "ok")
    assert r.product.name == "Те Гуань Инь"
    assert (r.shop.host, r.shop.name, r.shop.platform) == ("tea.example", "Лавка", "generic")


async def test_unknown_host_resolving_to_a_private_address_is_refused() -> None:
    async def private(host: str) -> list[str]:
        return ["10.0.0.5"]

    t = Transport((200, GENERIC, "https://tea.example/goods/tgy"))
    code, r = await ask(
        "https://tea.example/goods/tgy", deps(t, allow_unknown_hosts=True, resolve_dns=private)
    )
    assert (code, r.status) == (422, "host_not_allowed")
    assert r.meta.detail == {"reason": "ip_not_global"}
    assert t.calls == []


# --- ответ магазина ---------------------------------------------------------------------------


async def test_real_404_is_not_found() -> None:
    t = Transport((404, "<html><title>404</title></html>", MOYCHAY))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status) == (404, "not_found")
    assert r.url.canonical == MOYCHAY
    assert r.meta.egress == "direct"


async def test_soft_404_is_not_found_too() -> None:
    html = "<html><head><title>Страница не найдена</title></head><body><h1>404</h1></body></html>"
    code, r = await ask(REALCHINATEA, deps(Transport((200, html, REALCHINATEA))))
    assert (code, r.status) == (404, "not_found")
    assert r.meta.detail == {"soft": True}


async def test_redirect_to_a_category_is_not_found() -> None:
    t = Transport((200, "<h1>Шэн пуэр прессованный</h1>", "https://moychay.ru/catalog/puer"))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status) == (404, "not_found")
    assert r.meta.detail == {"redirected_to": "https://moychay.ru/catalog/puer"}


async def test_redirect_to_another_host_is_not_a_product_url() -> None:
    t = Transport((200, PAGE, "https://other.example/x"))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status) == (422, "not_a_product_url")
    assert r.meta.detail == {"redirected_to": "https://other.example/x"}


async def test_page_without_the_product_marker_is_not_a_card() -> None:
    html = (
        "<html><head><title>Шэн пуэр прессованный</title></head>"
        "<body><h1>Шэн пуэр прессованный</h1></body></html>"
    )
    code, r = await ask(MOYCHAY_CATEGORY, deps(Transport((200, html, MOYCHAY_CATEGORY))))
    assert (code, r.status) == (422, "not_a_product_url")
    assert r.meta.detail == {"reason": "no_product_marker"}


async def test_layout_drift_is_pending_with_candidates_for_the_postmortem() -> None:
    html = "<html><head><title>404</title></head><body><h1>Главная</h1></body></html>"
    code, r = await ask(REALCHINATEA, deps(Transport((200, html, REALCHINATEA))))
    assert (code, r.status, r.meta.reason) == (202, "pending", "layout_changed")
    assert r.meta.detail == {"candidates": [["h1", "Главная"], ["title", "404"]]}
    assert r.meta.retry_after_seconds == RETRY_AFTER_MINT_S


async def test_block_is_retried_once_through_a_proxy() -> None:
    t = Transport((403, "<title>Доступ ограничен</title>", MOYCHAY), (200, PAGE, MOYCHAY))
    code, r = await ask(MOYCHAY, deps(t, proxy=PROXY))
    assert (code, r.status) == (200, "ok")
    assert r.meta.egress == "proxy"
    assert [name for name, _ in r.meta.ledger] == ["shop.fetch", "shop.fetch_proxy"]
    assert [c["proxy"] for c in t.calls] == [None, PROXY]


async def test_block_without_a_proxy_is_pending() -> None:
    t = Transport((403, "", MOYCHAY))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status, r.meta.reason) == (202, "pending", "shop_blocked")
    assert r.meta.detail == {"http_status": 403}
    assert r.meta.retry_after_seconds == RETRY_AFTER_MINT_S
    assert len(t.calls) == 1


async def test_challenge_page_with_200_counts_as_a_block() -> None:
    t = Transport((200, "<html><title>Just a moment...</title></html>", MOYCHAY))
    code, r = await ask(MOYCHAY, deps(t))
    assert (code, r.status, r.meta.reason) == (202, "pending", "shop_blocked")


async def test_server_error_is_pending_with_a_short_retry() -> None:
    code, r = await ask(MOYCHAY, deps(Transport((502, "", MOYCHAY))))
    assert (code, r.status, r.meta.reason) == (202, "pending", "shop_error")
    assert r.meta.retry_after_seconds == RETRY_AFTER_FETCH_S


async def test_unreachable_shop_is_pending() -> None:
    code, r = await ask(MOYCHAY, deps(Transport(ShopUnreachable("dns"))))
    assert (code, r.status, r.meta.reason) == (202, "pending", "shop_unreachable")


async def test_deadline_is_504() -> None:
    code, r = await ask(MOYCHAY, deps(Transport(DeadlineExceeded("shop.fetch"))))
    assert (code, r.status, r.meta.reason) == (504, "deadline_exceeded", "deadline_exhausted")
    assert r.meta.retry_after_seconds is None
    assert [name for name, _ in r.meta.ledger] == ["shop.fetch"]


async def test_oversized_body_is_pending_not_a_crash() -> None:
    code, r = await ask(MOYCHAY, deps(Transport((200, PAGE, MOYCHAY)), max_bytes=100))
    assert (code, r.status, r.meta.reason) == (202, "pending", "body_too_large")


# --- реестр в деле ----------------------------------------------------------------------------


async def test_variant_parameter_survives_and_tracking_does_not() -> None:
    url = "https://chaline.ru/catalog/chay/x/221344/?utm_source=tg&oid=221352"
    html = "<html><head><title>x</title></head><body><h1>Шэн пуэр 200 г</h1></body></html>"
    t = Transport((200, html, url))
    code, r = await ask(url, deps(t))
    assert r.status == "ok"
    assert r.url.canonical == "https://chaline.ru/catalog/chay/x/221344/?oid=221352"
    assert t.calls[0]["url"] == r.url.canonical


async def test_spa_shop_is_fetched_prerendered_but_canonical_stays_clean() -> None:
    url = "https://teaworkshop.ru/product/puer-bodreishiy-2-2023-g"
    page = (FIXTURES / "teaworkshop.ru.html").read_text(encoding="utf-8")
    t = Transport((200, page, url + "?_escaped_fragment_="))
    code, r = await ask(url, deps(t))
    assert (code, r.status) == (200, "ok")
    assert r.product.name == "Пуэр Шу «Бодрейший No 2» 2023 г."
    assert r.url.canonical == url
    assert t.calls[0]["url"] == url + "?_escaped_fragment_="


@pytest.mark.parametrize("fixture", sorted(p.stem for p in FIXTURES.glob("*.html")))
async def test_every_fixture_goes_through_the_whole_path(fixture: str) -> None:
    """Реестр, маркер и экстрактор вместе: карточка каждого магазина даёт ok."""
    from tests.test_shops_extract import CARDS, PRODUCT_URLS  # noqa: PLC0415

    host = next(h for f, h, _ in CARDS if f == fixture)
    expected = next(n for f, _, n in CARDS if f == fixture)
    url = next(u for u in PRODUCT_URLS if host in u and _matches(u, fixture))
    page = (FIXTURES / f"{fixture}.html").read_text(encoding="utf-8")
    code, r = await ask(url, deps(Transport((200, page, url.split("?")[0]))))
    assert (code, r.status) == (200, "ok"), r.meta.detail
    assert r.product.name == expected
    assert r.shop.name


def _matches(url: str, fixture: str) -> bool:
    if fixture == "moschaitorg.ru":
        return "baj-khao" in url
    if fixture == "moschaitorg.ru-kenya":
        return "opa712" in url
    return True


async def test_silent_host_is_read_through_the_provider() -> None:
    """Хост молчит на TLS с нашего адреса — карточка приходит через scrape.do."""

    async def provider(url: str, *, timeout_ms: int) -> tuple[int, str, str]:
        return 200, PAGE, url

    t = Transport(ShopUnreachable("connect_timeout"))
    d = ShopDeps(cache=ProductCache(), fetcher=ShopFetcher(t, provider=provider))
    code, r = await handle_shop(ShopRequest(url=MOYCHAY), d)
    assert (code, r.status, r.meta.egress) == (200, "ok", "api")
    assert r.product.name == "Шен пуэр Мэнхай Лао, 2012"
    assert [name for name, _ in r.meta.ledger] == ["shop.fetch", "shop.fetch_api"]
