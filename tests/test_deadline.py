"""Дедлайн: срез от остатка, леджер на исключении, гард горячего пути."""

from __future__ import annotations

import asyncio
import math

import pytest

from mktlink.timing.deadline import (
    Deadline,
    DeadlineExceeded,
    OffRequestPathViolation,
    assert_off_request_path,
    bind,
    current,
    stage,
    unbind,
)


def test_slice_clamps_and_never_extends() -> None:
    dl = Deadline.start(1000, "t")
    # Просим больше остатка — получаем остаток минус резерв, не больше.
    assert dl.slice_ms(10_000, reserve_ms=0) <= 1000
    # Просим меньше — получаем именно столько.
    assert dl.slice_ms(50, reserve_ms=0) == 50
    # Резерв вычитается всегда.
    assert dl.slice_ms(math.inf, reserve_ms=400) <= 600


def test_slice_never_negative() -> None:
    dl = Deadline.start(100, "t")
    assert dl.slice_ms(math.inf, reserve_ms=10_000) == 0
    assert dl.slice_ms(50, reserve_ms=10_000) == 0


def test_afford_refuses_what_cannot_finish() -> None:
    dl = Deadline.start(1000, "t")
    assert dl.afford(floor_ms=100, reserve_ms=300)
    # Пол больше, чем остаток за вычетом резерва — не начинаем.
    assert not dl.afford(floor_ms=900, reserve_ms=300)


@pytest.mark.asyncio
async def test_stage_records_even_when_it_raises() -> None:
    """Ключевое правило: стадия, съевшая бюджет и упавшая, обязана быть в леджере."""
    dl = Deadline.start(1000, "t")
    with pytest.raises(RuntimeError):
        async with stage(dl, "boom", cap_ms=500, reserve_ms=0):
            raise RuntimeError("inner failure")
    assert "boom" in dl.ledger()


@pytest.mark.asyncio
async def test_stage_turns_its_own_timeout_into_deadline_exceeded() -> None:
    dl = Deadline.start(1000, "t")
    with pytest.raises(DeadlineExceeded) as ei:
        async with stage(dl, "slow", cap_ms=30, reserve_ms=0):
            await asyncio.sleep(5)
    assert ei.value.stage_name == "slow"
    assert "slow" in dl.ledger()


@pytest.mark.asyncio
async def test_stage_refuses_to_start_what_has_no_room() -> None:
    dl = Deadline.start(50, "t")
    with pytest.raises(DeadlineExceeded):
        async with stage(dl, "no-room", cap_ms=100, reserve_ms=10_000):
            pytest.fail("must not enter the body")


@pytest.mark.asyncio
async def test_stage_hands_out_what_it_allocated() -> None:
    dl = Deadline.start(1000, "t")
    async with stage(dl, "ok", cap_ms=120, reserve_ms=0) as ms:
        assert ms == 120


def test_ledger_aggregates_repeated_stages() -> None:
    dl = Deadline.start(1000, "t")
    dl.record("fetch", 10)
    dl.record("fetch", 15)
    dl.record("parse", 5)
    assert dl.ledger() == {"fetch": 25, "parse": 5}


def test_elapsed_and_remaining_are_complementary() -> None:
    dl = Deadline.start(1000, "t")
    assert dl.elapsed_ms + dl.remaining_ms <= 1000
    assert dl.remaining_ms > 0


def test_off_request_path_guard_is_quiet_outside_a_request() -> None:
    assert current() is None
    assert_off_request_path("proxy6 buy")  # не бросает


def test_off_request_path_guard_fires_inside_a_request() -> None:
    dl = Deadline.start(1000, "t")
    token = bind(dl)
    try:
        assert current() is dl
        with pytest.raises(OffRequestPathViolation) as ei:
            assert_off_request_path("proxy6 buy")
        assert "forge" in str(ei.value)
    finally:
        unbind(token)
    assert current() is None
