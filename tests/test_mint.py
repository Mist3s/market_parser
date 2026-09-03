"""Forge: протокол, коалесцирование, публикация jar только после реплея.

Браузера здесь нет и быть не может — он живёт в другом процессе и в этом
окружении не установлен. Проверяется всё остальное, а его интерфейс подменён.
"""

from __future__ import annotations

import asyncio

import pytest

from mktlink.egress.fingerprint import FingerprintDrift
from mktlink.egress.jars import JarStore
from mktlink.mint.mint import (
    REQUIRED_COOKIES,
    WARMUP_MS,
    estimate_eta_ms,
    mint,
)
from mktlink.mint.rpc import MINT_HARD_CAP_MS, Coalescer, Frame, Request
from mktlink.store.db import connect, init_db
from mktlink.timing.deadline import Deadline, OffRequestPathViolation, bind, unbind

FF_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"


@pytest.fixture
def jars(tmp_path):
    init_db(tmp_path / "f.sqlite")
    c = connect(tmp_path / "f.sqlite")
    yield JarStore(c)
    c.close()


class FakeBrowser:
    def __init__(self, cookies="yandexuid=1; __Secure-ETC=x", ua=FF_UA):
        self.cookies, self.ua = cookies, ua
        self.calls: list[dict] = []

    async def cookies_for(self, url, *, proxy_url, wait_ms):
        self.calls.append({"url": url, "proxy": proxy_url, "wait_ms": wait_ms})
        return self.cookies, self.ua


async def yes(jar, proxy_url):
    return True


async def no(jar, proxy_url):
    return False


# --- протокол -------------------------------------------------------------------


def test_request_round_trips() -> None:
    r = Request(op="mint", marketplace="ym", proxy_id=7, request_id="abc")
    assert Request.decode(r.encode()) == r
    assert r.key == ("mint", "ym", 7)


def test_frame_round_trips() -> None:
    f = Frame(kind="progress", eta_ms=9000)
    assert Frame.decode(f.encode()) == f


def test_the_mint_cap_is_unrelated_to_the_response_budget() -> None:
    """Минтинг идёт вне запроса, поэтому ручка его не ограничивает."""
    assert MINT_HARD_CAP_MS > 15_000


# --- коалесцирование ---------------------------------------------------------------


async def test_three_callers_start_one_browser_not_three() -> None:
    """При 350 МБ на инстанс это разница между работой и OOM."""
    started = 0

    async def work():
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)
        return "jar"

    c = Coalescer()
    key = ("mint", "ozon", 7)
    results = await asyncio.gather(*(c.run(key, work) for _ in range(3)))
    assert started == 1
    assert results == ["jar"] * 3


async def test_a_finished_job_does_not_block_the_next_one() -> None:
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        return runs

    c = Coalescer()
    key = ("mint", "ym", 1)
    assert await c.run(key, work) == 1
    assert await c.run(key, work) == 2


async def test_eta_is_read_from_memory_not_over_the_socket() -> None:
    """Вызов без дедлайна после его истечения превратил бы 202 в краевой 504."""
    c = Coalescer()
    key = ("mint", "ozon", 7)
    assert c.eta(key) == MINT_HARD_CAP_MS, "разумный дефолт без единого I/O"
    c.note_eta(key, 9000)
    assert c.eta(key) == 9000


def test_eta_estimate_is_derived_from_the_measured_cost() -> None:
    for mp in ("ozon", "wb", "ym"):
        assert estimate_eta_ms(mp) > WARMUP_MS[mp]
    assert estimate_eta_ms("ozon") > estimate_eta_ms("ym"), "прогрев Ozon дороже"


# --- минтинг -----------------------------------------------------------------------


async def test_a_jar_is_published_only_after_replay_is_verified(jars) -> None:
    b = FakeBrowser()
    res = await mint(b, jars, marketplace="ym", proxy_id=7,
                     proxy_url="http://p:1@h:8000", verify_replay=yes)
    assert res.ok
    assert jars.get("ym", 7) is not None


async def test_a_failed_replay_leaves_no_usable_jar(jars) -> None:
    """Отказ ГЛАВНОЙ ставки дизайна обязан быть видим сразу."""
    res = await mint(FakeBrowser(), jars, marketplace="ym", proxy_id=7,
                     proxy_url="http://p:1@h:8000", verify_replay=no)
    assert not res.ok and "replay" in res.detail
    assert jars.get("ym", 7) is None, "непроверенный jar читателю не виден"


async def test_missing_required_cookies_fail_the_mint(jars) -> None:
    b = FakeBrowser(cookies="irrelevant=1")
    res = await mint(b, jars, marketplace="ozon", proxy_id=7,
                     proxy_url="http://p:1@h:8000", verify_replay=yes)
    assert not res.ok and "__Secure-ETC" in res.detail
    assert REQUIRED_COOKIES["wb"] == frozenset(), "WB cookie не требует"


async def test_a_browser_older_than_the_forged_version_breaks_the_mint(jars) -> None:
    """Подделывать версию новее установленной нельзя.

    Отпечаток из будущего — аномалия сам по себе, поэтому ломается сборка,
    а не success rate через три недели.
    """
    older = FakeBrowser(ua=FF_UA.replace("152", "140"))
    with pytest.raises(FingerprintDrift, match="does not exist yet"):
        await mint(older, jars, marketplace="ym", proxy_id=7,
                   proxy_url="http://p:1@h:8000", verify_replay=yes)


async def test_a_gap_within_tolerance_is_accepted(jars) -> None:
    """Равенства мажоров не бывает: замер дал Firefox 152 против цели 147."""
    res = await mint(FakeBrowser(), jars, marketplace="ym", proxy_id=7,
                     proxy_url="http://p:1@h:8000", verify_replay=yes)
    assert res.ok, res.detail


async def test_the_region_is_forced_into_the_cookies_too(jars) -> None:
    """Иначе ответ придёт для региона, куда геолоцируется прокси."""
    b = FakeBrowser(cookies="yandexuid=1; yandex_gid=2")
    await mint(b, jars, marketplace="ym", proxy_id=7,
               proxy_url="http://p:1@h:8000", verify_replay=yes)
    jar = jars.get("ym", 7)
    assert jar is not None and "yandex_gid=213" in jar.cookie_header
    assert "yandex_gid=2;" not in jar.cookie_header


async def test_the_warmup_wait_is_a_named_measurable_tunable(jars) -> None:
    b = FakeBrowser()
    await mint(b, jars, marketplace="ozon", proxy_id=7,
               proxy_url="http://p:1@h:8000", verify_replay=yes)
    assert b.calls[0]["wait_ms"] == WARMUP_MS["ozon"] == 6000
    assert b.calls[0]["proxy"] == "http://p:1@h:8000"


async def test_minting_inside_a_request_is_refused(jars) -> None:
    """Граница процессов физическая, но гард стоит и в коде."""
    dl = Deadline.start(15000, "t")
    token = bind(dl)
    try:
        with pytest.raises(OffRequestPathViolation):
            await mint(FakeBrowser(), jars, marketplace="ym", proxy_id=7,
                       proxy_url="http://p:1@h:8000", verify_replay=yes)
    finally:
        unbind(token)


async def test_the_observed_ua_is_stored_not_the_profile_constant(jars) -> None:
    rotated = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0"
    await mint(FakeBrowser(ua=rotated), jars, marketplace="ym", proxy_id=7,
               proxy_url="http://p:1@h:8000", verify_replay=yes)
    jar = jars.get("ym", 7)
    assert jar is not None and jar.user_agent == rotated
