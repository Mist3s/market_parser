"""Проход по лестнице: слияние, деградация профиля, отказ начинать невлезающее."""

from __future__ import annotations

import asyncio

from mktlink.budget import Rung, plan
from mktlink.marketplaces.base import Context, RungResult, merge, run_ladder
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.timing.deadline import Deadline

CTX = Context(
    marketplace="ym",
    canonical_url="https://market.yandex.ru/card/slug/4382957723",
    ids={"sku_id": "4382957723"},
    anchor_ids=frozenset({"4382957723"}),
)


class Scripted:
    """Экстрактор с заготовленным ответом на каждую ступень."""

    marketplace = "ym"

    def __init__(self, **by_name: RungResult) -> None:
        self.by_name = by_name
        self.calls: list[str] = []
        self.caps: dict[str, int] = {}

    def rung_fn(self, rung: Rung):
        async def fn(dl, ctx, cap_ms, prev):
            self.calls.append(rung.name)
            self.caps[rung.name] = cap_ms
            return self.by_name.get(rung.name, RungResult(verdict=Verdict.SILENT_EMPTY))

        return fn


def good(name="Пюре", seller="ООО «Ромашка»") -> RungResult:
    return RungResult(
        verdict=Verdict.OK,
        name=name,
        seller_name=seller,
        seller_status=SellerStatus.RESOLVED,
        seller_source="ym:state:a.b",
    )


async def test_first_complete_rung_stops_the_ladder() -> None:
    ex = Scripted(**{"ym.pdp_html": good()})
    res, last = await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert res.verdict is Verdict.OK
    assert ex.calls == ["ym.pdp_html"], "дальше идти незачем"
    assert last == "ym.pdp_html"


async def test_a_half_success_is_completed_by_the_next_rung_not_restarted() -> None:
    """Вторая ступень даёт имя, когда первая дала только продавца."""
    ex = Scripted(
        **{
            "ym.pdp_html": RungResult(
                verdict=Verdict.PARTIAL,
                seller_name="ООО «Ромашка»",
                seller_status=SellerStatus.RESOLVED,
                seller_source="ym:state:a",
            ),
            "ym.offers_page": RungResult(verdict=Verdict.PARTIAL, name="Пюре Semper"),
        }
    )
    res, _ = await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert res.name == "Пюре Semper"
    assert res.seller_name == "ООО «Ромашка»", "добытое первой ступенью не потеряно"
    assert res.verdict is Verdict.OK


async def test_merge_keeps_the_resolved_seller_over_a_later_unknown() -> None:
    a = good()
    b = RungResult(verdict=Verdict.PARTIAL, name="Другое имя")
    assert merge(a, b).seller_status is SellerStatus.RESOLVED
    assert merge(a, b).name == "Пюре", "первое непустое имя выигрывает"


async def test_captcha_stops_the_ladder_instead_of_burning_another_slot() -> None:
    ex = Scripted(**{"ym.pdp_html": RungResult(verdict=Verdict.CAPTCHA)})
    res, _ = await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert ex.calls == ["ym.pdp_html"]
    assert res.verdict is Verdict.CAPTCHA


async def test_all_four_rungs_run_when_none_succeeds() -> None:
    ex = Scripted()
    await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert ex.calls == [r.name for r in plan(15000, "ym", 0).rungs]
    assert len(ex.calls) == 4


async def test_caps_come_from_the_planner_not_from_the_rung() -> None:
    ex = Scripted()
    await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    p = plan(15000, "ym", 0)
    assert [ex.caps[r.name] for r in p.rungs] == list(p.caps)


async def test_a_slow_request_simply_gets_fewer_rungs() -> None:
    """Автоматическая деградация профиля вместо очереди и отказа."""
    ex = Scripted()
    dl = Deadline.start(14600, "t")
    # Съедаем большую часть бюджета до начала лестницы.
    dl.at_monotonic -= 12.0
    await run_ladder(dl, ex, CTX, 15000, 0)
    assert 0 <= len(ex.calls) < 4, ex.calls


async def test_a_rung_that_cannot_finish_is_not_started() -> None:
    started = False

    class Never:
        marketplace = "ym"

        def rung_fn(self, rung):
            async def fn(dl, ctx, cap_ms, prev):
                nonlocal started
                started = True
                await asyncio.sleep(0)
                return RungResult(verdict=Verdict.OK)

            return fn

    dl = Deadline.start(14600, "t")
    dl.at_monotonic -= 14.5  # остатка нет даже на пол первой ступени
    res, _ = await run_ladder(dl, Never(), CTX, 15000, 0)
    assert not started, "слот прокси не потрачен впустую"
    assert res.verdict is Verdict.BUDGET_EXHAUSTED


async def test_partial_is_reported_when_only_the_name_was_found() -> None:
    ex = Scripted(**{"ym.pdp_html": RungResult(verdict=Verdict.PARTIAL, name="Пюре")})
    res, _ = await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert res.verdict is Verdict.PARTIAL
    assert res.name == "Пюре" and res.seller_name is None


async def test_a_card_with_no_offers_is_a_complete_answer() -> None:
    ex = Scripted(
        **{
            "ym.pdp_html": RungResult(
                verdict=Verdict.OK, name="Пюре", seller_status=SellerStatus.NO_OFFERS
            )
        }
    )
    res, _ = await run_ladder(Deadline.start(14600, "t"), ex, CTX, 15000, 0)
    assert res.verdict is Verdict.OK
    assert ex.calls == ["ym.pdp_html"], "искать продавца дальше незачем — их нет"


async def test_ozon_ladder_has_two_rungs_at_full_budget() -> None:
    """Enrich-ступень выбрасывается первой: у Ozon гард 5000 не оставляет ей места."""
    p = plan(15000, "ozon", 0)
    assert [r.name for r in p.rungs] == ["ozon.composer_replay", "ozon.pdp_html_replay"]
    assert "ozon.seller_widget" not in [r.name for r in p.rungs]
