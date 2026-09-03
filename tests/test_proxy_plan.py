"""Жизненный цикл прокси: ленивая закупка, замена, продление, тариф.

Всё, что тратит деньги, проходит через чистую функцию, поэтому проверяется
здесь, а не наблюдением за счётом proxy6.
"""

from __future__ import annotations

import pytest

from mktlink.proxy6.plan import (
    BAD_WINDOW,
    MIN_REMAINING_S,
    NO_ESCALATION,
    QUORUM_TO_RETIRE,
    RENEW_HORIZON_S,
    Command,
    Health,
    Inventory,
    Op,
    Situation,
    plan_bootstrap,
    plan_buy,
    plan_renewals,
    plan_replacement,
    plan_tier,
    should_retire,
    usable,
)

NOW = 1_800_000_000
FAR = NOW + 30 * 86400


def inv(descr: str, **kw) -> Inventory:
    base = dict(p6_id=1, descr_raw=descr, version=3, unixtime_end=FAR, active=True)
    base.update(kw)
    return Inventory(**base)  # type: ignore[arg-type]


def sit(*inventory: Inventory, **kw) -> Situation:
    base = dict(now=NOW, inventory=list(inventory), spent_kop_30d=0)
    base.update(kw)
    return Situation(**base)  # type: ignore[arg-type]


def ops(cmds: list[Command]) -> list[Op]:
    return [c.op for c in cmds]


# --- ленивая закупка ------------------------------------------------------------


def test_empty_account_buys_exactly_one() -> None:
    """Пула нет. Один адрес, и всё."""
    cmds = plan_bootstrap(sit())
    buys = [c for c in cmds if c.op is Op.BUY]
    assert len(buys) == 1
    assert buys[0].version == 3, "по умолчанию IPv4 Shared"
    assert buys[0].period_days == 7


def test_an_existing_usable_proxy_is_reused_not_supplemented() -> None:
    """Заказчик запретил провизионирование впрок."""
    cmds = plan_bootstrap(sit(inv("mp1.mp.s.a.R.ru.g01", never_renew_local=True)))
    assert Op.BUY not in ops(cmds)


def test_a_proxy_about_to_expire_does_not_count_as_usable() -> None:
    """Он истечёт посреди запроса, и отказ будет выглядеть как вина прокси."""
    soon = inv("mp1.mp.s.a.R.ru.g01", unixtime_end=NOW + MIN_REMAINING_S - 1,
               never_renew_local=True)
    assert not usable(soon, NOW)
    assert Op.BUY in ops(plan_bootstrap(sit(soon)))


def test_a_retired_proxy_does_not_count_as_usable() -> None:
    assert not usable(inv("mp1.mp.s.r.X.ru.g01"), NOW)


# --- чужие адреса -----------------------------------------------------------------


def test_foreign_proxies_are_counted_and_never_touched() -> None:
    """Переименование чужого — захват оплаченной кем-то собственности."""
    cmds = plan_bootstrap(sit(inv("someones-own-proxy")))
    assert ops(cmds).count(Op.NOTE_FOREIGN) == 1
    assert Op.SETDESCR not in ops(cmds)
    assert Op.DELETE not in ops(cmds)
    assert Op.PROLONG not in ops(cmds)


def test_a_foreign_proxy_does_not_satisfy_the_need_for_one_of_ours() -> None:
    cmds = plan_bootstrap(sit(inv("someones-own-proxy")))
    assert Op.BUY in ops(cmds), "чужой адрес нас не обслуживает"


# --- инверсия дефолта при потере стора ----------------------------------------------


def test_a_renewable_proxy_unknown_locally_is_not_blessed() -> None:
    """После полной потери стора мы обязаны молчать, а не благословлять."""
    cmds = plan_bootstrap(sit(inv("mp1.mp.s.a.R.ru.g01", never_renew_local=False)))
    assert Op.ADOPT_PENDING in ops(cmds)


def test_a_known_renewable_proxy_is_left_alone() -> None:
    cmds = plan_bootstrap(sit(inv("mp1.mp.s.a.R.ru.g01", never_renew_local=True)))
    assert Op.ADOPT_PENDING not in ops(cmds)


def test_a_malformed_descr_is_treated_as_never_renew() -> None:
    """Безопасная сторона ошибки — потерять адрес, а не продлить неизвестное."""
    cmds = plan_bootstrap(sit(inv("mp1.GARBAGE")))
    assert Op.MARK_NEVER_RENEW in ops(cmds)


def test_an_orphaned_purchase_is_adopted_not_bought_again() -> None:
    """Ответ на buy потерялся, а адрес пришёл. Второй раз платить незачем."""
    cmds = plan_bootstrap(sit(inv("mp1.ord.260902.7Q3XK9")))
    assert Op.BUY in ops(cmds), "сам по себе nonce ещё не годен к работе"
    setd = [c for c in cmds if c.op is Op.SETDESCR]
    assert setd and setd[0].descr == "adopt-orphan"


# --- замена --------------------------------------------------------------------------


def test_bad_on_one_marketplace_is_not_enough_to_retire() -> None:
    """Иначе Ozon сжёг бы каждый купленный адрес, включая годные для Маркета."""
    bad_ozon = inv(
        "mp1.mp.s.a.R.ru.g01",
        health={
            "ozon": Health(ok_n=0, bad_n=BAD_WINDOW),
            "ym": Health(ok_n=BAD_WINDOW, bad_n=0),
            "wb": Health(ok_n=BAD_WINDOW, bad_n=0),
        },
    )
    assert not should_retire(bad_ozon)


def test_a_quorum_of_marketplaces_retires_the_proxy() -> None:
    dead = inv(
        "mp1.mp.s.a.R.ru.g01",
        health={
            "ozon": Health(ok_n=0, bad_n=BAD_WINDOW),
            "ym": Health(ok_n=0, bad_n=BAD_WINDOW),
            "wb": Health(ok_n=BAD_WINDOW, bad_n=0),
        },
    )
    assert should_retire(dead)
    assert QUORUM_TO_RETIRE == 2


def test_a_small_sample_never_decides() -> None:
    """Порог, набирающийся за 20 минут, — это скорость реакции, и её надо знать."""
    thin = Health(ok_n=0, bad_n=BAD_WINDOW - 1)
    assert not thin.decided and not thin.bad
    assert Health(ok_n=0, bad_n=BAD_WINDOW).bad


def test_the_never_renew_mark_is_written_before_anything_else() -> None:
    """Падение сразу после метки теряет адрес, но не продлевает плохой."""
    cmds = plan_replacement(sit(), inv("mp1.mp.s.a.R.ru.g01"))
    assert cmds[0].op is Op.MARK_NEVER_RENEW
    assert ops(cmds).index(Op.MARK_NEVER_RENEW) < ops(cmds).index(Op.SETDESCR)
    assert ops(cmds).index(Op.MARK_NEVER_RENEW) < ops(cmds).index(Op.BUY)


def test_replacement_buys_exactly_one() -> None:
    cmds = plan_replacement(sit(), inv("mp1.mp.s.a.R.ru.g01"))
    assert ops(cmds).count(Op.BUY) == 1


# --- продление -------------------------------------------------------------------------


def test_an_expiring_renewable_proxy_is_prolonged() -> None:
    soon = inv("mp1.mp.s.a.R.ru.g01", unixtime_end=NOW + RENEW_HORIZON_S - 1,
               never_renew_local=False)
    cmds = plan_renewals(sit(soon))
    assert ops(cmds) == [Op.PROLONG]


def test_a_far_off_proxy_is_left_alone() -> None:
    assert plan_renewals(sit(inv("mp1.mp.s.a.R.ru.g01"))) == []


@pytest.mark.parametrize(
    ("descr", "local"),
    [
        ("mp1.mp.s.a.X.ru.g01", False),  # живой descr запрещает
        ("mp1.mp.s.a.R.ru.g01", True),  # локальная запись запрещает
        ("mp1.mp.s.a.X.ru.g01", True),  # оба
    ],
)
def test_either_side_of_the_veto_alone_stops_renewal(descr: str, local: bool) -> None:
    """Двусторонний veto: любой источник в одиночку запрещает трату."""
    expiring = inv(descr, unixtime_end=NOW + 3600, never_renew_local=local)
    assert plan_renewals(sit(expiring)) == []


def test_restoring_an_old_backup_cannot_resurrect_a_burnt_proxy() -> None:
    """Локальная запись потеряна, но живой descr всё ещё говорит X."""
    burnt = inv("mp1.mp.s.a.X.ru.g01", unixtime_end=NOW + 3600, never_renew_local=False)
    assert plan_renewals(sit(burnt)) == []


def test_a_retired_proxy_is_never_prolonged() -> None:
    retired = inv("mp1.mp.s.r.X.ru.g01", unixtime_end=NOW + 3600)
    assert plan_renewals(sit(retired)) == []


# --- деньги -----------------------------------------------------------------------------


def test_the_cap_stops_a_buy_before_it_is_attempted() -> None:
    broke = sit(spent_kop_30d=59_500, quote_kop=870, cap_kop_30d=60_000)
    assert plan_buy(broke, marketplace=None, reason="x") == []
    assert Op.BUY not in ops(plan_bootstrap(broke))


def test_the_cap_also_stops_renewals() -> None:
    """Иначе продление обходило бы потолок, ради которого он и стоит."""
    expiring = inv("mp1.mp.s.a.R.ru.g01", unixtime_end=NOW + 3600)
    assert plan_renewals(sit(expiring, spent_kop_30d=59_500, quote_kop=870)) == []


def test_a_buy_loop_cannot_form_because_each_plan_asks_for_one() -> None:
    """Планировщик не умеет попросить больше одного за раз, по построению."""
    for _ in range(5):
        cmds = plan_buy(sit(), marketplace=None, reason="x")
        assert len(cmds) == 1


# --- тариф --------------------------------------------------------------------------------


def test_yandex_market_never_escalates() -> None:
    """SmartCaptcha — челлендж, а не блок по репутации адреса.

    Выделенный датацентровый IPv4 решения выдать челлендж не меняет, поэтому
    платить вчетверо тут не за что.
    """
    assert "ym" in NO_ESCALATION
    hopeless = Health(ok_n=0, bad_n=BAD_WINDOW)
    assert plan_tier("ym", hopeless, None) == "hold"


def test_a_bad_shared_tier_escalates_for_ozon() -> None:
    assert plan_tier("ozon", Health(ok_n=0, bad_n=BAD_WINDOW), None) == "escalate"


def test_a_thin_sample_holds_rather_than_spends() -> None:
    assert plan_tier("ozon", Health(ok_n=1, bad_n=2), None) == "hold"


def test_a_healthy_shared_tier_deescalates_away_from_the_paid_arm() -> None:
    good = Health(ok_n=BAD_WINDOW, bad_n=0)
    assert plan_tier("ozon", good, Health(ok_n=BAD_WINDOW, bad_n=0)) == "deescalate"


def test_a_paid_arm_that_bought_nothing_is_abandoned() -> None:
    """Мешает не сосед по адресу, а сам датацентр — платить больше бессмысленно."""
    bad = Health(ok_n=0, bad_n=BAD_WINDOW)
    assert plan_tier("ozon", bad, bad) == "deescalate"


def test_a_paid_arm_that_worked_is_kept() -> None:
    assert plan_tier("ozon", Health(ok_n=0, bad_n=BAD_WINDOW),
                     Health(ok_n=BAD_WINDOW, bad_n=0)) == "hold"


def test_escalation_changes_only_what_the_next_purchase_buys() -> None:
    cmds = plan_buy(sit(escalate=frozenset({"ozon"})), marketplace="ozon", reason="x")
    assert cmds[0].version == 4
    cmds = plan_buy(sit(escalate=frozenset({"ozon"})), marketplace="ym", reason="x")
    assert cmds[0].version == 3
