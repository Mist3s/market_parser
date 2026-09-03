"""HTTP-слой: статусы, заголовки, балкхед."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mktlink.api.app import create_app
from mktlink.api.routes import Deps, Extraction
from mktlink.constants import (
    CLIENT_TIMEOUT_MARGIN_MS,
    RESPONSE_BUDGET_DEFAULT_MS,
    RETRY_AFTER_MINT_S,
)
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.store.cache import ProductCache

YM = "https://market.yandex.ru/card/pyure-semper/4382957723"

GOOD = Extraction(
    verdict=Verdict.OK,
    name="Пюре Semper",
    seller_name="ООО «Ромашка»",
    seller_status=SellerStatus.RESOLVED,
    seller_source="ym:state:widgets.DefaultOffer.shop.name",
    rung="Y1",
)


def client(ladder=None) -> TestClient:
    async def ok(dl, c, budget_ms):
        return GOOD

    return TestClient(create_app(Deps(cache=ProductCache(), ladder=ladder or ok)))


def test_post_returns_the_envelope() -> None:
    r = client().post("/v1/product", json={"url": YM})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["seller"]["name"] == "ООО «Ромашка»"
    assert body["url"]["canonical"] == YM
    assert r.headers["X-Request-Id"]


def test_get_form_is_supported_with_the_same_contract() -> None:
    r = client().get("/v1/product", params={"url": YM})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_request_id_is_echoed_when_supplied() -> None:
    r = client().post("/v1/product", json={"url": YM}, headers={"X-Request-Id": "abc123"})
    assert r.headers["X-Request-Id"] == "abc123"
    assert r.json()["request_id"] == "abc123"


def test_pending_carries_retry_after_header() -> None:
    async def blocked(dl, c, budget_ms):
        return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")

    r = client(ladder=blocked).post("/v1/product", json={"url": YM})
    assert r.status_code == 202
    assert r.headers["Retry-After"] == str(RETRY_AFTER_MINT_S)
    assert r.json()["url"]["canonical"] == YM


def test_extra_field_is_422() -> None:
    r = client().post("/v1/product", json={"url": YM, "nope": 1})
    assert r.status_code == 422


def test_out_of_scope_host_is_422_with_the_envelope() -> None:
    r = client().post("/v1/product", json={"url": "https://samokat.ru/p/1"})
    assert r.status_code == 422
    assert r.json()["status"] == "host_not_allowed"


def test_prices_are_never_cached_by_intermediaries() -> None:
    r = client().post("/v1/product", json={"url": YM})
    assert r.headers["Cache-Control"] == "no-store"


def test_health_and_ready() -> None:
    c = client()
    assert c.get("/healthz").json()["ok"] is True
    ready = c.get("/readyz").json()
    # Числа берутся из констант, а не вписаны: этот тест проверяет, что
    # /readyz ОТДАЁТ обслуживаемый потолок, а не что потолок равен 15 с.
    assert ready["budget_ms"] == RESPONSE_BUDGET_DEFAULT_MS
    assert (
        ready["client_timeout_hint_ms"]
        == RESPONSE_BUDGET_DEFAULT_MS + CLIENT_TIMEOUT_MARGIN_MS
    )
