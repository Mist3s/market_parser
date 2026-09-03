"""Исходящий клиент и jar: реплей наблюдённого UA, атрибутация егресса."""

from __future__ import annotations

import time

import pytest

from mktlink.egress.client import MAX_HTML_BYTES, BodyTooLarge, EgressClient
from mktlink.egress.jars import MAX_AGE_S, Jar, JarStore
from mktlink.store.db import connect, init_db
from mktlink.timing.deadline import Deadline


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "j.sqlite")
    c = connect(tmp_path / "j.sqlite")
    yield c
    c.close()


def a_jar(**kw) -> Jar:
    base = dict(
        marketplace="ym",
        proxy_id=7,
        cookie_header="yandexuid=1; yandex_gid=213",
        user_agent="Mozilla/5.0 (Macintosh; rv:133.0) Gecko/20100101 Firefox/133.0",
        firefox_major=133,
        minted_at=int(time.time()),
    )
    base.update(kw)
    return Jar(**base)


# --- jar ---------------------------------------------------------------------


def test_an_unverified_jar_is_invisible_to_readers(conn) -> None:
    """Между минтингом и подтверждением реплея jar есть, но им не пользуются."""
    store = JarStore(conn)
    store.put_unverified(a_jar())
    assert store.get("ym", 7) is None, "ставка на реплей ещё не проверена"
    store.publish("ym", 7)
    assert store.get("ym", 7) is not None


def test_a_jar_is_keyed_by_marketplace_and_proxy_not_by_host(conn) -> None:
    """Cookie привязаны к IP, который их получил."""
    store = JarStore(conn)
    store.put_unverified(a_jar(proxy_id=7))
    store.publish("ym", 7)
    assert store.get("ym", 7) is not None
    assert store.get("ym", 8) is None, "другой адрес — другой jar"
    assert store.get("ozon", 7) is None, "другой маркетплейс — другой jar"


def test_a_stale_jar_is_not_offered(conn) -> None:
    store = JarStore(conn)
    store.put_unverified(a_jar(minted_at=int(time.time()) - MAX_AGE_S - 60))
    store.publish("ym", 7)
    assert store.get("ym", 7) is None


def test_dropping_a_jar_removes_it_and_the_memory_copy(conn) -> None:
    store = JarStore(conn)
    store.put_unverified(a_jar())
    store.publish("ym", 7)
    assert store.get("ym", 7) is not None
    store.drop("ym", 7)
    assert store.get("ym", 7) is None


def test_reminting_resets_verification(conn) -> None:
    """Новый jar снова непубликован: реплей надо подтвердить заново."""
    store = JarStore(conn)
    store.put_unverified(a_jar())
    store.publish("ym", 7)
    store.put_unverified(a_jar(cookie_header="yandexuid=2"))
    assert store.get("ym", 7) is None


# --- клиент -------------------------------------------------------------------


def transport(status=200, body="ok", sink=None):
    async def send(url, *, headers, proxy, impersonate, timeout_ms):
        if sink is not None:
            sink.append({"url": url, "headers": headers, "proxy": proxy,
                         "impersonate": impersonate, "timeout_ms": timeout_ms})
        return status, body

    return send


async def test_replay_uses_the_observed_ua_and_the_jar_cookies() -> None:
    sink: list[dict] = []
    c = EgressClient(transport(sink=sink))
    jar = a_jar()
    await c.fetch(
        Deadline.start(15000, "t"), "https://market.yandex.ru/card/s/1",
        marketplace="ym", cap_ms=2600, reserve_ms=440, jar=jar, proxy_url="http://p:1@h:8000",
    )
    sent = sink[0]
    assert sent["headers"]["User-Agent"] == jar.user_agent, "наблюдённый, не константа"
    assert sent["headers"]["Cookie"] == jar.cookie_header
    assert sent["impersonate"] == "firefox133", "Camoufox это Firefox"
    assert "sec-ch-ua" not in sent["headers"], "Firefox их не шлёт"


async def test_egress_is_labelled_so_the_scorer_can_ignore_direct() -> None:
    c = EgressClient(transport())
    dl = Deadline.start(15000, "t")
    via_proxy = await c.fetch(dl, "https://x/y", marketplace="ym", cap_ms=1000,
                              reserve_ms=0, proxy_url="http://p:1@h:8000")
    direct = await c.fetch(dl, "https://x/y", marketplace="ym", cap_ms=1000, reserve_ms=0)
    assert via_proxy.egress == "proxy"
    assert direct.egress == "direct"


async def test_the_timeout_comes_from_the_deadline_not_from_a_literal() -> None:
    sink: list[dict] = []
    c = EgressClient(transport(sink=sink))
    dl = Deadline.start(1000, "t")
    await c.fetch(dl, "https://x/y", marketplace="ym", cap_ms=99_999, reserve_ms=0)
    assert sink[0]["timeout_ms"] <= 1000, "cap урезан остатком, а не принят как есть"


async def test_an_oversized_body_is_refused() -> None:
    c = EgressClient(transport(body="x" * (MAX_HTML_BYTES + 1)))
    with pytest.raises(BodyTooLarge):
        await c.fetch(Deadline.start(15000, "t"), "https://x/y", marketplace="ym",
                      cap_ms=1000, reserve_ms=0)


async def test_a_fetch_is_a_named_budget_stage() -> None:
    c = EgressClient(transport())
    dl = Deadline.start(15000, "t")
    await c.fetch(dl, "https://x/y", marketplace="ym", cap_ms=1000, reserve_ms=0,
                  stage_name="ym.pdp_html")
    assert "ym.pdp_html" in dl.ledger()
