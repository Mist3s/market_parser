"""Требование 7: защита входа. Учёт в кредитах, а не в запросах."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mktlink.api.apikey import (
    COST,
    Admission,
    RateLimited,
    Unauthorized,
    register_key,
)
from mktlink.api.app import create_app
from mktlink.api.routes import Deps, Extraction
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db

YM = "https://market.yandex.ru/card/pyure/4382957723"
GOOD = Extraction(
    verdict=Verdict.OK, name="Пюре", seller_name="ООО «Ромашка»",
    seller_status=SellerStatus.RESOLVED, seller_source="ym:state:a", rung="Y1",
)


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "k.sqlite")
    c = connect(tmp_path / "k.sqlite")
    register_key(c, "client-1", "s3cret", quota=100)
    yield c
    c.close()


def client(conn, admission=None) -> TestClient:
    async def ok(dl, c, budget_ms):
        return GOOD

    return TestClient(create_app(Deps(cache=ProductCache(), ladder=ok), admission=admission))


# --- ключи ---------------------------------------------------------------------


def test_a_valid_key_authenticates(conn) -> None:
    a = Admission(conn)
    assert a.authenticate("s3cret").key_id == "client-1"


def test_an_unknown_or_absent_key_is_refused(conn) -> None:
    a = Admission(conn)
    for bad in (None, "", "wrong"):
        with pytest.raises(Unauthorized):
            a.authenticate(bad)


def test_a_disabled_key_is_refused(conn) -> None:
    conn.execute("UPDATE api_key SET disabled = 1")
    with pytest.raises(Unauthorized):
        Admission(conn).authenticate("s3cret")


# --- кредиты --------------------------------------------------------------------


def test_operations_cost_what_they_actually_consume(conn) -> None:
    """Попадание в кэш не тратит прокси; сброс кэша тратит гарантированно."""
    assert COST["cache_hit"] < COST["cold"] < COST["force_refresh"]
    assert COST["force_refresh"] == 4 * COST["cold"], "сознательно карательно"


def test_polling_a_job_is_free(conn) -> None:
    """Иначе вежливый клиент выбирает квоту опросами и получает 429 на деле."""
    a = Admission(conn)
    key = a.authenticate("s3cret")
    for _ in range(100):
        a.charge(key, "job_poll")
    assert a.usage("client-1") == 0


def test_the_daily_quota_is_enforced(conn) -> None:
    a = Admission(conn)
    key = a.authenticate("s3cret")
    for _ in range(10):
        a.charge(key, "cold")  # 10 × 10 = 100 = квота
    with pytest.raises(RateLimited) as ei:
        a.charge(key, "cold")
    assert ei.value.scope == "daily"


def test_a_burst_cannot_eat_the_day_at_once(conn) -> None:
    a = Admission(conn, rpm_credits=30)
    key = a.authenticate("s3cret")
    a.charge(key, "cold")
    a.charge(key, "cold")
    a.charge(key, "cold")
    with pytest.raises(RateLimited) as ei:
        a.charge(key, "cold")
    assert ei.value.scope == "per-minute"
    assert ei.value.retry_after_s == 60


def test_overcharging_is_refunded_so_cheap_requests_stay_cheap(conn) -> None:
    """Списываем по худшему до работы, иначе всегда падающий абьюзер не платит."""
    a = Admission(conn)
    key = a.authenticate("s3cret")
    a.charge(key, "cold")
    assert a.usage("client-1") == 10
    a.refund(key, 10, "cache_hit")
    assert a.usage("client-1") == 1


def test_a_refund_never_inflates_the_charge(conn) -> None:
    a = Admission(conn)
    key = a.authenticate("s3cret")
    a.charge(key, "cold")
    a.refund(key, 10, "force_refresh")
    assert a.usage("client-1") == 10, "возврат только вниз"


# --- через HTTP ---------------------------------------------------------------------


def test_without_admission_the_endpoint_is_open(conn) -> None:
    """Обратная совместимость: защита включается явно."""
    assert client(conn).post("/v1/product", json={"url": YM}).status_code == 200


def test_with_admission_a_key_is_required(conn) -> None:
    c = client(conn, Admission(conn))
    r = c.post("/v1/product", json={"url": YM})
    assert r.status_code == 401
    assert r.json()["status"] == "unauthorized"


def test_a_valid_key_passes_and_is_charged(conn) -> None:
    a = Admission(conn)
    c = client(conn, a)
    r = c.post("/v1/product", json={"url": YM}, headers={"X-Api-Key": "s3cret"})
    assert r.status_code == 200
    assert a.usage("client-1") == COST["cold"]


def test_exhausting_the_quota_returns_429_with_retry_after(conn) -> None:
    a = Admission(conn, rpm_credits=25)
    c = client(conn, a)
    for _ in range(2):
        c.post("/v1/product", json={"url": YM}, headers={"X-Api-Key": "s3cret"})
    r = c.post("/v1/product", json={"url": YM}, headers={"X-Api-Key": "s3cret"})
    assert r.status_code == 429
    assert r.json()["status"] == "rate_limited"
    assert int(r.headers["Retry-After"]) > 0


def test_a_rejected_url_costs_less_than_a_cold_fetch(conn) -> None:
    a = Admission(conn)
    c = client(conn, a)
    c.post("/v1/product", json={"url": "https://bit.ly/x"}, headers={"X-Api-Key": "s3cret"})
    assert a.usage("client-1") == COST["unsupported_marketplace"]
