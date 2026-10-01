"""Гард спейсинга: ключ-пара, самофинансирование, переживание рестарта, часы."""

from __future__ import annotations

import time

import pytest

from mktlink.budget import LADDER, plan, stage_reserve_ms
from mktlink.constants import MIN_INTERVAL_MS, RESPONSE_BUDGET_MAX_MS
from mktlink.egress.spacing import Reservation, peek, reserve
from mktlink.egress.spacing import wait as spacing_wait
from mktlink.marketplaces.base import Context, RungResult, run_ladder
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.store.db import connect, init_db
from mktlink.timing.deadline import Deadline, DeadlineExceeded


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "s.sqlite"
    init_db(path)
    c = connect(path)
    yield c
    c.close()


def test_first_call_never_waits(conn) -> None:
    r = reserve(conn, "ozon", 1, now_ms=1_000_000)
    assert r.wait_ms == 0 and not r.binds


def test_second_call_waits_the_full_interval(conn) -> None:
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    r = reserve(conn, "ozon", 1, now_ms=1_000_000)
    assert r.wait_ms == MIN_INTERVAL_MS["ozon"] == 5000


def test_a_later_call_waits_only_the_remainder(conn) -> None:
    reserve(conn, "ym", 1, now_ms=1_000_000)
    r = reserve(conn, "ym", 1, now_ms=1_000_400)
    assert r.wait_ms == 200, "600 интервал минус 400 прошедших"


def test_after_the_interval_there_is_no_wait(conn) -> None:
    reserve(conn, "ym", 1, now_ms=1_000_000)
    r = reserve(conn, "ym", 1, now_ms=1_000_600)
    assert r.wait_ms == 0


def test_the_key_is_the_pair_not_the_proxy(conn) -> None:
    """Общий на прокси ограничитель уничтожил бы лестницу Я.Маркета.

    Он дал бы ym ozon'овские 5 секунд: четыре ступени и три гарда по 5000 —
    это 15 секунд обязательного ожидания внутри окна в 14 075 мс.
    """
    reserve(conn, "ozon", 7, now_ms=1_000_000)
    # Тот же прокси, другой маркетплейс — ждать нечего.
    assert reserve(conn, "ym", 7, now_ms=1_000_000).wait_ms == 0
    assert reserve(conn, "wb", 7, now_ms=1_000_000).wait_ms == 0

    # Доказательство считается на ТОМ бюджете, на котором оно верно, и это
    # сказано вслух. При 15 с общий гард дал бы 15 000 обязательного
    # ожидания против 4 675 запаса — лестницы не осталось бы. При 30 с
    # запаса хватает (19 675), то есть утверждение перестаёт быть верным
    # арифметически, хотя ключ по паре по-прежнему правильный выбор:
    # спейсинг защищает АДРЕС, и общий счётчик на прокси означал бы, что
    # запрос к WB задерживает запрос к Ozon без причины.
    ym = plan(15000, "ym", 0)
    if_shared = MIN_INTERVAL_MS["ozon"] * len(ym.guards)
    assert if_shared > ym.net_ms - sum(ym.caps), "иначе тест не доказывает утверждение"

    # А это — то, что верно при любом бюджете и потому проверяется отдельно.
    assert reserve(conn, "ozon", 7, now_ms=1_000_100).wait_ms > 0, (
        "тот же прокси и тот же маркетплейс обязаны ждать"
    )


def test_different_proxies_do_not_share_a_slot(conn) -> None:
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    assert reserve(conn, "ozon", 2, now_ms=1_000_000).wait_ms == 0


def test_state_survives_a_restart(conn, tmp_path) -> None:
    """Иначе перезапуск разрешил бы выстрел сразу после предыдущего."""
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    conn.close()

    again = connect(tmp_path / "s.sqlite")
    try:
        assert peek(again, "ozon", 1, now_ms=1_000_000) == 5000
    finally:
        again.close()


def test_peek_does_not_consume_a_slot(conn) -> None:
    assert peek(conn, "ym", 1, now_ms=1_000_000) == 0
    assert peek(conn, "ym", 1, now_ms=1_000_000) == 0
    reserve(conn, "ym", 1, now_ms=1_000_000)
    assert peek(conn, "ym", 1, now_ms=1_000_000) == 600


def test_reservation_happens_before_the_wait_not_after(conn) -> None:
    """Иначе два одновременных вызывающих оба увидят свободный слот."""
    a = reserve(conn, "ozon", 1, now_ms=1_000_000)
    b = reserve(conn, "ozon", 1, now_ms=1_000_000)
    assert a.wait_ms == 0 and b.wait_ms == 5000, "второй встал в очередь, а не выстрелил"


def test_refusal_does_not_take_the_slot(conn) -> None:
    """Регрессия: запрос, который не пойдёт, отодвигал следующий.

    Каждый отказ занимал слот ещё на интервал, и серия отказов копила долг:
    чем больше отказов, тем дольше ждал первый запрос, который мог бы пройти.
    """
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    for _ in range(3):
        r = reserve(conn, "ozon", 1, now_ms=1_000_000, max_wait_ms=4999)
        assert r.refused and r.wait_ms == 5000
    assert peek(conn, "ozon", 1, now_ms=1_000_000) == 5000, "отказы долга не копят"


def test_wait_up_to_the_limit_is_admitted_and_booked(conn) -> None:
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    r = reserve(conn, "ozon", 1, now_ms=1_000_000, max_wait_ms=5000)
    assert not r.refused and r.wait_ms == 5000
    assert peek(conn, "ozon", 1, now_ms=1_000_000) == 10_000


def test_the_slot_is_kept_on_the_wall_clock(conn) -> None:
    """Регрессия: монотонные часы отсчитываются от загрузки машины.

    Момент, записанный до перезагрузки хоста, оказывался впереди на весь
    прежний аптайм, и пара получала отказ на каждый запрос, пока новый аптайм
    не догонит старый.
    """
    before = time.time_ns() // 1_000_000
    reserve(conn, "ym", 1)
    after = time.time_ns() // 1_000_000
    row = conn.execute(
        "SELECT next_allowed_ms FROM spacing WHERE mp = 'ym' AND proxy_id = 1"
    ).fetchone()
    interval = MIN_INTERVAL_MS["ym"]
    assert before + interval <= row["next_allowed_ms"] <= after + interval


def test_slot_written_by_the_old_clock_does_not_block(conn) -> None:
    """Строки прежней редакции — монотонные мс, то есть далёкое прошлое."""
    conn.execute(
        "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES ('ozon', 1, ?)",
        (int(time.monotonic() * 1000) + 5000,),
    )
    assert reserve(conn, "ozon", 1).wait_ms == 0


def test_debt_no_admitted_request_could_leave_is_dropped(conn) -> None:
    """Часы прыгнули назад — или строку писали другие часы."""
    ceiling = RESPONSE_BUDGET_MAX_MS + MIN_INTERVAL_MS["ozon"]
    conn.execute(
        "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES ('ozon', 1, ?)",
        (1_000_000 + ceiling + 1,),
    )
    assert peek(conn, "ozon", 1, now_ms=1_000_000) == 0
    assert reserve(conn, "ozon", 1, now_ms=1_000_000).wait_ms == 0
    assert peek(conn, "ozon", 1, now_ms=1_000_000) == MIN_INTERVAL_MS["ozon"]


def test_debt_up_to_the_ceiling_is_honoured(conn) -> None:
    ceiling = RESPONSE_BUDGET_MAX_MS + MIN_INTERVAL_MS["ozon"]
    conn.execute(
        "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES ('ozon', 1, ?)",
        (1_000_000 + ceiling,),
    )
    assert peek(conn, "ozon", 1, now_ms=1_000_000) == ceiling
    assert reserve(conn, "ozon", 1, now_ms=1_000_000).wait_ms == ceiling


async def test_wait_is_a_budget_stage_and_can_refuse(conn) -> None:
    """Ожидание — такая же статья бюджета, как сетевой вызов."""
    reserve(conn, "ozon", 1, now_ms=1_000_000)
    r = reserve(conn, "ozon", 1, now_ms=1_000_000)

    dl = Deadline.start(1000, "t")
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await spacing_wait(dl, r, reserve_ms=300)
    assert time.monotonic() - started < 0.2, "отказ сразу, а не после сна до края бюджета"


async def test_wait_that_fits_is_served_not_refused() -> None:
    """Регрессия: таймер стадии, равный сну, срабатывал раньше него."""
    for _ in range(5):
        dl = Deadline.start(5000, "t")
        await spacing_wait(dl, Reservation("avito", -2, 60), reserve_ms=440)
        assert dl.ledger()["spacing"] >= 60


async def test_wait_is_a_noop_when_the_slot_is_free(conn) -> None:
    dl = Deadline.start(1000, "t")
    r = reserve(conn, "ym", 1, now_ms=1_000_000)
    await spacing_wait(dl, r, reserve_ms=300)
    assert dl.ledger() == {}


class _Recorder:
    """Лестница, которая только отмечает, что её ступень запустили."""

    def __init__(self, mp: str) -> None:
        self.marketplace = mp
        self.ran: list[str] = []

    def rung_fn(self, rung):
        async def fn(dl, ctx, cap_ms, prev):
            self.ran.append(rung.name)
            return RungResult(
                verdict=Verdict.OK, name="ч", seller_name="п", seller_status=SellerStatus.RESOLVED
            )

        return fn


@pytest.mark.parametrize(("mp", "budget"), [("ozon", 15000), ("ym", 15000), ("avito", 5000)])
async def test_admission_floor_is_exactly_where_the_ladder_starts(mp: str, budget: int) -> None:
    """Допуск ожидания в проводке резервирует ``stage_reserve_ms + plan.floor()``.

    Это обязано совпадать с порогом, при котором лестница начинает первую
    ступень: меньше — ожидание, после которого не влезает ни одна ступень,
    больше — отказ запросу, который успел бы.
    """
    need = stage_reserve_ms(mp) + plan(budget, mp, 0).floor()
    for slack_ms, starts in ((50, True), (-50, False)):
        dl = Deadline(time.monotonic() + (need + slack_ms) / 1000, "t", budget)
        lane = _Recorder(mp)
        await run_ladder(dl, lane, Context(mp, "https://example.test/", {}), budget, hops=0)
        assert bool(lane.ran) is starts, (slack_ms, need)


def test_self_financing_holds_only_for_ym_and_that_is_the_point() -> None:
    """Асимметрия несёт смысл, поэтому проверяется как таблица."""
    holds = {
        mp: MIN_INTERVAL_MS[mp]
        <= min(r.floor_ms for r in LADDER[mp] if r.kind in ("replay", "enrich"))
        for mp in ("ozon", "wb", "ym")
    }
    assert holds == {"ozon": False, "wb": False, "ym": True}


def test_ym_guard_never_exceeds_the_shortest_rung_it_follows() -> None:
    """Следствие самофинансирования: успешная ступень оплачивает следующий гард."""
    shortest = min(r.floor_ms for r in LADDER["ym"] if r.kind in ("replay", "enrich"))
    assert MIN_INTERVAL_MS["ym"] <= shortest == 600
