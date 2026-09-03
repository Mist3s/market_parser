"""Тождество бюджета и совпадение с леджером §3.3 спецификации.

Это не «тест на всякий случай»: тождество — единственное, что удерживает
потолок ответа. Если оно рухнет, ответ сможет превысить бюджет, и никакой
дедлайн об этом не узнает.
"""

from __future__ import annotations

import pytest

from mktlink.budget import (
    LADDER,
    BudgetTooSmall,
    Plan,
    derive,
    plan,
    request_ladder,
    stage_reserve_ms,
)
from mktlink.constants import (
    BUDGET_FLOOR_MS,
    CLIENT_TIMEOUT_MARGIN_MS,
    MARKETPLACES,
    MIN_INTERVAL_MS,
    MINT_HARD_CAP_MS,
    PARSE_MS,
    REDIRECT_CAP_MS,
    RESPONSE_BUDGET_MAX_MS,
    SAFETY_MS,
    SER_MS,
    TAIL_MS,
)

BUDGETS = (3675, 5000, 8000, 12000, 15000)
HOPS = (0, 1, 2, 3)


def _combos():
    for b in BUDGETS:
        for mp in MARKETPLACES:
            for hops in HOPS:
                yield b, mp, hops


@pytest.mark.parametrize(("budget", "mp", "hops"), list(_combos()))
def test_identity_holds_or_ladder_does_not_exist(budget: int, mp: str, hops: int) -> None:
    """Σ(cap) + Σ(guards) + резидуал == NET_MS, для каждой достижимой комбинации."""
    net = derive(budget, mp, hops).net_ms
    try:
        p = plan(budget, mp, hops)
    except BudgetTooSmall:
        # Допустимый исход: при малом бюджете и трёх хопах лестницы нет вовсе.
        # Но она обязана существовать при полном бюджете без хопов.
        assert budget < RESPONSE_BUDGET_MAX_MS or hops > 0
        return

    assert p.total() == net, f"identity broken: {p.total()} != {net}"
    assert len(p.guards) == len(p.rungs) - 1, "конвенция гардов n−1"
    assert p.prewait_ms >= 0
    assert p.caps[0] == min(p.rungs[0].ceil_ms, net) >= p.rungs[0].floor_ms
    for rung, cap in zip(p.rungs, p.caps, strict=True):
        assert rung.floor_ms <= cap <= rung.ceil_ms, f"{rung.name}: {cap}"
    assert not any(r.kind in ("mint", "render") for r in p.rungs), "браузера на пути запроса нет"


@pytest.mark.parametrize(("budget", "mp", "hops"), list(_combos()))
def test_derive_matches_the_closed_forms(budget: int, mp: str, hops: int) -> None:
    caps = derive(budget, mp, hops)
    assert caps.edge_read_ms == budget - 100
    assert caps.app_ms == budget - 400
    assert caps.io_ms == budget - 615
    assert caps.edge_read_ms < budget
    assert caps.app_ms < caps.edge_read_ms
    offset = 785 if mp in ("ozon", "wb") else 925
    assert caps.net_ms == budget - offset - 550 * hops


# Клетки леджера §3.3: (бюджет, маркетплейс, хопы) -> (cap'ы ступеней, гарды, резидуал).
LEDGER = {
    (5000, "ozon", 0): ((1500,), (), 2715),
    (15000, "ozon", 0): ((1500, 1800), (5000,), 5915),
    (5000, "wb", 0): ((1200,), (), 3015),
    (15000, "wb", 0): ((1200, 1800, 1400), (2000, 2000), 5815),
    (5000, "ym", 0): ((2600,), (), 1475),
    (15000, "ym", 0): ((2600, 2400, 2600, 1800), (600, 600, 600), 2875),
    # Единственная клетка, где первая ступень зажата бюджетом, а не своим потолком.
    (5000, "ym", 3): ((2425,), (), 0),
}


@pytest.mark.parametrize(("key", "expected"), sorted(LEDGER.items()))
def test_plan_reproduces_the_specification_ledger(key, expected) -> None:
    budget, mp, hops = key
    exp_caps, exp_guards, exp_prewait = expected
    p = plan(budget, mp, hops)
    assert p.caps == exp_caps
    assert p.guards == exp_guards
    assert p.prewait_ms == exp_prewait


def test_stage_reserve_is_its_definition() -> None:
    for mp in MARKETPLACES:
        assert stage_reserve_ms(mp) == PARSE_MS[mp] + SER_MS + TAIL_MS + SAFETY_MS
    assert stage_reserve_ms("ozon") == stage_reserve_ms("wb") == 300
    assert stage_reserve_ms("ym") == 440


def test_budget_floor_is_exactly_what_makes_the_hardest_case_fit() -> None:
    """Пол — не круглое число, а арифметика худшей комбинации: ym и три хопа."""
    assert BUDGET_FLOOR_MS == (
        485 + REDIRECT_CAP_MS + stage_reserve_ms("ym") + LADDER["ym"][0].floor_ms
    )
    # На самом полу лестница ym с тремя хопами существует ровно в одну ступень.
    p = plan(BUDGET_FLOOR_MS, "ym", 3)
    assert p.caps == (LADDER["ym"][0].floor_ms,)
    assert p.prewait_ms == 0
    # На один миллисекунд ниже её уже нет.
    with pytest.raises(BudgetTooSmall):
        plan(BUDGET_FLOOR_MS - 1, "ym", 3)


def test_self_financing_guard_is_an_expected_table_not_a_truth() -> None:
    """Гард самофинансируется только у Я.Маркета, и это несёт смысл.

    Асимметрия намеренная: у ym гард (600) не превышает пола самой короткой
    ступени пути запроса, поэтому ожидание всегда оплачено недотраченным
    временем предыдущей ступени. У Ozon и WB это не так, и там гард — реальная
    статья расхода. Любое изменение любого из шести чисел ломает этот тест,
    что и требуется.
    """
    actual = {
        mp: MIN_INTERVAL_MS[mp]
        <= min(r.floor_ms for r in LADDER[mp] if r.kind in ("replay", "enrich"))
        for mp in MARKETPLACES
    }
    assert actual == {"ozon": False, "wb": False, "ym": True}


def test_mint_never_reaches_the_request_path() -> None:
    for mp in MARKETPLACES:
        assert any(r.kind == "mint" for r in LADDER[mp]), "минтинг объявлен"
        assert not any(r.kind == "mint" for r in request_ladder(mp)), "но не на пути запроса"
    assert MINT_HARD_CAP_MS > RESPONSE_BUDGET_MAX_MS


def test_render_is_opt_in_and_only_for_ym() -> None:
    assert not any(r.kind == "render" for r in request_ladder("ym"))
    with_render = request_ladder("ym", with_render=True)
    assert [r.kind for r in with_render] == ["replay", "replay", "replay", "render", "enrich"]
    for mp in ("ozon", "wb"):
        assert request_ladder(mp, with_render=True) == request_ladder(mp)


def test_render_option_still_balances() -> None:
    p = plan(15000, "ym", 0, with_render=True)
    assert p.total() == p.net_ms


def test_client_timeout_hint() -> None:
    assert CLIENT_TIMEOUT_MARGIN_MS == 500
    assert RESPONSE_BUDGET_MAX_MS + CLIENT_TIMEOUT_MARGIN_MS == 15_500


def test_prewait_below_guard_is_exactly_when_spacing_can_force_a_202() -> None:
    """Резидуал < MIN_INTERVAL ⟺ спейсинг может не влезть. Проверяем обе стороны."""
    binding = plan(5000, "ozon", 0)
    assert binding.prewait_ms < MIN_INTERVAL_MS["ozon"]  # 2715 < 5000

    roomy = plan(15000, "ym", 0)
    assert roomy.prewait_ms >= MIN_INTERVAL_MS["ym"]  # 2875 >= 600


def test_unknown_marketplace_is_a_key_error_not_a_silent_default() -> None:
    with pytest.raises(KeyError):
        derive(15000, "detmir", 0)


def test_plan_is_pure() -> None:
    a = plan(15000, "ym", 0)
    b = plan(15000, "ym", 0)
    assert a == b
    assert isinstance(a, Plan)
