"""Боевая сборка раскрутки: провайдер, сокращение, кэш и отказы без сети."""

from urllib.parse import parse_qs, urlsplit

import pytest
from curl_cffi.requests.exceptions import Timeout

from mktlink.api.routes import resolve
from mktlink.api.schemas import ResolveRequest
from mktlink.api.wiring import build_deps
from mktlink.egress.resolve import ScrapeDoResolver
from mktlink.settings import Settings
from mktlink.store.db import connect, init_db

SHORT = "https://ozon.ru/t/8M3J7yH"
PDP = "https://www.ozon.ru/product/chay-483430098/?__rr=1"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / "r.sqlite"
    init_db(path)
    conn = connect(path)
    cfg = Settings(_env_file=None, scrapedo_token="test-token", scrapedo_shorten_via="clck",
                   redis_url=None)
    calls = []

    async def shorten(url, **kwargs):
        assert url == SHORT
        calls.append("shorten")
        return "https://clck.ru/TestShort"

    monkeypatch.setattr("mktlink.egress.scrapedo.shorten", shorten)
    yield build_deps(cfg, conn), conn, calls
    conn.close()


async def test_configured_resolver_uses_provider_and_caches_final_url(configured, monkeypatch):
    deps, conn, calls = configured

    async def send(self, url, headers, timeout_ms):
        query = parse_qs(urlsplit(url).query)
        assert query["url"] == ["https://clck.ru/TestShort"]
        assert query["render"] == ["true"]
        assert query["token"] == ["test-token"]
        assert 4000 < timeout_ms < 30000
        assert headers["X-Rnet-Allow-Redirects"] == "1"
        calls.append("provider")
        return 200, "<html>not parsed</html>", {"Scrape.do-Resolved-Url": PDP}

    monkeypatch.setattr(ScrapeDoResolver, "_send", send)
    for expected_cache in ("miss", "hit"):
        code, body = await resolve(ResolveRequest(url=SHORT, max_wait_ms=30000), deps)
        assert code == 200
        assert body.url.canonical == "https://www.ozon.ru/product/chay-483430098/"
        assert body.product.name is None
        assert body.seller.status == "not_requested"
        assert body.meta.cache == expected_cache
    assert calls == ["shorten", "provider"]
    assert conn.execute("SELECT count(*) FROM proxy_health").fetchone()[0] == 0


@pytest.mark.parametrize("status,headers,expected", [
    (200, {}, (503, "capacity_exhausted")),
    (200, {"Scrape.do-Resolved-Url": "https://market.yandex.ru/product/123"},
     (422, "not_a_product_url")),
    (200, {"Scrape.do-Resolved-Url": "http://127.0.0.1/admin"},
     (422, "not_a_product_url")),
    (200, {"Scrape.do-Resolved-Url": "https://ozon.ru/captcha"},
     (504, "unwind_challenged")),
    (200, {"Scrape.do-Resolved-Url": "https://ozon.ru/t/same"},
     (422, "not_a_product_url")),
    (400, {}, (503, "capacity_exhausted")),
    (403, {}, (504, "unwind_challenged")),
])
async def test_provider_failure_is_not_500_or_cached(configured, monkeypatch, status,
                                                   headers, expected):
    deps, _, _ = configured

    async def send(self, url, req_headers, timeout_ms):
        return status, ('{"Message":["disabled the target domain"]}'
                        if status == 400 else "<html>blocked</html>"), headers

    monkeypatch.setattr(ScrapeDoResolver, "_send", send)
    code, body = await resolve(ResolveRequest(url=SHORT), deps)
    assert (code, body.status) == expected
    assert deps.unwound.get(SHORT) is None
    if status == 400:
        assert body.meta.reason == "provider_domain_disabled"
    assert "disabled the target domain" not in body.model_dump_json()


async def test_provider_timeout_remains_a_deadline(configured, monkeypatch):
    deps, _, _ = configured

    async def send(self, url, headers, timeout_ms):
        raise Timeout("sensitive upstream URL")

    monkeypatch.setattr(ScrapeDoResolver, "_send", send)
    code, body = await resolve(ResolveRequest(url=SHORT), deps)
    assert (code, body.status) == (504, "deadline_exceeded")
    assert "sensitive" not in body.model_dump_json()


async def test_yandex_resolves_without_clck_and_preserves_offer(configured, monkeypatch):
    deps, _, calls = configured
    target = "https://market.yandex.ru/card/chay/101814267477?sku=101814267477&offerid=offer123"

    async def send(self, url, headers, timeout_ms):
        query = parse_qs(urlsplit(url).query)
        assert query["url"] == ["https://market.yandex.ru/cc/TestShort"]
        assert "render" not in query
        return 200, "", {"scrape.do-resolved-url": target}

    monkeypatch.setattr(ScrapeDoResolver, "_send", send)
    code, body = await resolve(ResolveRequest(url="https://market.yandex.ru/cc/TestShort"), deps)
    assert code == 200
    assert parse_qs(urlsplit(body.url.canonical).query)["offerid"] == ["offer123"]
    assert calls == []
