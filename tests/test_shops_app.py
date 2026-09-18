"""HTTP-слой ``/v1/shop``: конверт, заголовки, учёт кредитов."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mktlink.api.apikey import COST, Admission, register_key
from mktlink.api.app import create_app
from mktlink.api.routes import Deps
from mktlink.constants import RETRY_AFTER_MINT_S
from mktlink.shops.fetch import ShopFetcher
from mktlink.shops.service import ShopDeps
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db
from tests.test_shops_service import Transport

MOYCHAY = "https://moychay.ru/catalog/puer/shen_puer_pressovannyj/menhay-lao-2012"
PAGE = (Path(__file__).parent / "fixtures" / "shops" / "moychay.ru.html").read_text(
    encoding="utf-8"
)


def client(transport: Transport | None = None, admission: Admission | None = None) -> TestClient:
    async def never(dl, c, budget_ms):  # маркетплейсный путь здесь не нужен
        raise AssertionError("ladder must not run")

    shop_deps = ShopDeps(
        cache=ProductCache(),
        fetcher=ShopFetcher(transport or Transport((200, PAGE, MOYCHAY))),
    )
    return TestClient(
        create_app(
            Deps(cache=ProductCache(), ladder=never), admission=admission, shop_deps=shop_deps
        )
    )


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "k.sqlite")
    c = connect(tmp_path / "k.sqlite")
    register_key(c, "client-1", "s3cret", quota=100)
    yield c
    c.close()


def test_post_returns_the_shop_envelope() -> None:
    r = client().post("/v1/shop", json={"url": MOYCHAY})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["product"] == {"name": "Шен пуэр Мэнхай Лао, 2012", "source": "h1"}
    assert body["shop"] == {"host": "moychay.ru", "name": "Мойчай.ру", "platform": "inertia"}
    assert body["url"]["canonical"] == MOYCHAY
    assert r.headers["X-Request-Id"] == body["request_id"]
    assert r.headers["Cache-Control"] == "no-store"


def test_get_has_the_same_contract() -> None:
    r = client().get("/v1/shop", params={"url": MOYCHAY})
    assert r.status_code == 200
    assert r.json()["product"]["name"] == "Шен пуэр Мэнхай Лао, 2012"


def test_get_with_a_bad_budget_is_422_in_the_envelope() -> None:
    r = client().get("/v1/shop", params={"url": MOYCHAY, "max_wait_ms": 100})
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_budget"
    assert r.json()["url"]["submitted"] == MOYCHAY


def test_unknown_shop_is_422() -> None:
    r = client().post("/v1/shop", json={"url": "https://tea.example/product/x"})
    assert r.status_code == 422
    assert r.json()["status"] == "shop_not_supported"


def test_pending_carries_retry_after() -> None:
    r = client(Transport((403, "", MOYCHAY))).post("/v1/shop", json={"url": MOYCHAY})
    assert r.status_code == 202
    assert r.json()["meta"]["reason"] == "shop_blocked"
    assert r.headers["Retry-After"] == str(RETRY_AFTER_MINT_S)


def test_with_admission_a_key_is_required(conn) -> None:
    r = client(admission=Admission(conn)).post("/v1/shop", json={"url": MOYCHAY})
    assert r.status_code == 401
    assert r.json()["status"] == "unauthorized"


def test_cold_card_costs_shop_cold_and_a_hit_costs_a_hit(conn) -> None:
    a = Admission(conn)
    c = client(admission=a)
    key = {"X-Api-Key": "s3cret"}
    assert c.post("/v1/shop", json={"url": MOYCHAY}, headers=key).status_code == 200
    assert a.usage("client-1") == COST["shop_cold"]
    assert c.post("/v1/shop", json={"url": MOYCHAY}, headers=key).status_code == 200
    assert a.usage("client-1") == COST["shop_cold"] + COST["cache_hit"]


def test_marketplace_endpoint_is_untouched() -> None:
    """Существующий контракт не знает о магазинах: чужой хост — прежний отказ."""
    r = client().post("/v1/product", json={"url": MOYCHAY})
    assert r.status_code == 422
    assert r.json()["status"] == "host_not_allowed"
