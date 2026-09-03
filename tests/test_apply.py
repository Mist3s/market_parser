"""Исполнитель: заявка до вызова, отсутствие повторов, изоляция метки."""

from __future__ import annotations

import pytest

from mktlink.proxy6.apply import Applier
from mktlink.proxy6.client import Proxy6Client
from mktlink.proxy6.descr import is_nonce, unpack
from mktlink.proxy6.plan import Command, Op
from mktlink.store.db import connect, init_db, is_never_renew


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "a.sqlite")
    c = connect(tmp_path / "a.sqlite")
    yield c
    c.close()


class Fake:
    """Записывает вызовы, отвечает по сценарию."""

    def __init__(self, **responses):
        self.calls: list[tuple[str, str]] = []
        self.responses = responses

    async def __call__(self, url: str) -> dict:
        method = url.split("/api/KEY/")[1].split("/")[0]
        self.calls.append((method, url))
        return self.responses.get(method, {"status": "yes"})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def url_for(self, method: str) -> str:
        return next(u for m, u in self.calls if m == method)


BOUGHT = {
    "status": "yes",
    "list": {"11": {"id": "11", "version": "3", "ip": "1.2.3.4", "host": "h",
                    "port": "8000", "user": "u", "pass": "p",
                    "unixtime_end": "1900000000", "descr": "x"}},
}


def applier(conn, fake) -> Applier:
    return Applier(Proxy6Client("KEY", transport=fake), conn)


async def test_buy_records_the_intent_before_calling(conn) -> None:
    """Потерянный ответ обязан быть виден денежному потолку."""
    fake = Fake(getprice={"status": "yes", "price": "8.70"}, buy=BOUGHT)
    res = await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    assert res[0].ok
    row = conn.execute("SELECT status, kop, nonce FROM proxy_spend").fetchone()
    assert row["status"] == "confirmed" and row["kop"] == 870
    assert is_nonce(row["nonce"]), "заявка помечена nonce для разбора потери"


async def test_the_buy_carries_a_nonce_and_is_never_repeated(conn) -> None:
    fake = Fake(getprice={"status": "yes", "price": "8.70"}, buy=BOUGHT)
    await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    assert fake.methods().count("buy") == 1, "повтор здесь стоит денег"
    assert "descr=mp1.ord." in fake.url_for("buy")
    assert "auto_prolong" not in fake.url_for("buy")


async def test_the_class_tag_replaces_the_nonce_after_landing(conn) -> None:
    fake = Fake(getprice={"status": "yes", "price": "8.70"}, buy=BOUGHT)
    await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    d = unpack(fake.url_for("setdescr").split("new=")[1].split("&")[0])
    assert (d.mk, d.role, d.renew) == ("xx", "w", "R"), "принят, но не назначен"


async def test_price_is_quoted_before_the_purchase(conn) -> None:
    fake = Fake(getprice={"status": "yes", "price": "8.70"}, buy=BOUGHT)
    await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    assert fake.methods().index("getprice") < fake.methods().index("buy")


async def test_a_price_typo_is_stopped_by_the_schema_before_it_becomes_spend(conn) -> None:
    """1.40 ₽/сут для v3: 7 дней -> максимум 980 копеек."""
    fake = Fake(getprice={"status": "yes", "price": "99.00"}, buy=BOUGHT)
    res = await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    assert not res[0].ok
    assert "buy" not in fake.methods(), "до вызова дело не дошло"


async def test_insufficient_balance_freezes_everything_after_it(conn) -> None:
    """Частичное продление хуже полного отказа: парк истекает неравномерно."""
    fake = Fake(getprice={"status": "no", "error_id": 400, "error": "balance"})
    res = await applier(conn, fake).apply(
        [Command(Op.BUY, version=3, period_days=7), Command(Op.BUY, version=3, period_days=7)]
    )
    assert len(res) == 1 and not res[0].ok
    assert "frozen" in res[0].detail


async def test_condemn_commits_without_any_money_in_the_transaction(conn) -> None:
    """Денежный потолок не имеет права откатить именно эту запись."""
    fake = Fake()
    res = await applier(conn, fake).apply([Command(Op.MARK_NEVER_RENEW, 42, reason="captcha")])
    assert res[0].ok
    assert is_never_renew(conn, 42)
    assert conn.execute("SELECT COUNT(*) AS n FROM proxy_spend").fetchone()["n"] == 0
    assert fake.methods() == [], "локальная метка сети не требует"


async def test_prolong_refuses_a_condemned_proxy(conn) -> None:
    _seed(conn)
    conn.execute("UPDATE proxy SET never_renew = 1 WHERE p6_id = 11")
    fake = Fake()
    res = await applier(conn, fake).apply([Command(Op.PROLONG, 11, period_days=7)])
    assert not res[0].ok
    assert "prolong" not in fake.methods()


async def test_a_duplicate_prolong_is_refused_by_the_database(conn) -> None:
    """Свидетель — срок ДО операции; дубль отвергает уникальный индекс."""
    _seed(conn)
    fake = Fake()
    a = applier(conn, fake)
    assert (await a.apply([Command(Op.PROLONG, 11, period_days=7)]))[0].ok
    second = await a.apply([Command(Op.PROLONG, 11, period_days=7)])
    assert not second[0].ok
    assert fake.methods().count("prolong") == 1, "второй раз не платим"


async def test_delete_refuses_anything_not_retired(conn) -> None:
    _seed(conn, descr="mp1.mp.s.a.R.ru.g01")
    fake = Fake()
    res = await applier(conn, fake).apply([Command(Op.DELETE, 11)])
    assert not res[0].ok
    assert "delete" not in fake.methods(), "удаление необратимо и ничего не покупает"


async def test_delete_allows_a_retired_and_condemned_proxy(conn) -> None:
    _seed(conn, descr="mp1.mp.s.r.X.ru.g01")
    fake = Fake(delete={"status": "yes", "count": 1})
    res = await applier(conn, fake).apply([Command(Op.DELETE, 11)])
    assert res[0].ok and "delete" in fake.methods()


async def test_retiring_writes_role_r_and_flag_x_together(conn) -> None:
    _seed(conn, descr="mp1.mp.s.a.R.ru.g01")
    fake = Fake()
    await applier(conn, fake).apply([Command(Op.SETDESCR, 11, descr="retire")])
    d = unpack(fake.url_for("setdescr").split("new=")[1].split("&")[0])
    assert d.role == "r" and d.renew == "X"


async def test_a_foreign_proxy_is_only_counted(conn) -> None:
    fake = Fake()
    res = await applier(conn, fake).apply([Command(Op.NOTE_FOREIGN, 99, descr="someones")])
    assert res[0].ok
    assert fake.methods() == [], "ни одного вызова к API"
    assert conn.execute("SELECT COUNT(*) AS n FROM foreign_ack").fetchone()["n"] == 1


async def test_adopt_pending_sets_both_flags(conn) -> None:
    _seed(conn)
    await applier(conn, Fake()).apply([Command(Op.ADOPT_PENDING, 11)])
    row = conn.execute(
        "SELECT never_renew, adopt_pending FROM proxy WHERE p6_id = 11"
    ).fetchone()
    assert (row["never_renew"], row["adopt_pending"]) == (1, 1)


def _seed(conn, descr: str = "mp1.mp.s.a.R.ru.g01") -> None:
    conn.execute(
        "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state, term_end)"
        " VALUES (11, '1.2.3.4', 'h', 8000, 'u', 'p', 3, ?, 'active', unixepoch() + 86400)",
        (descr,),
    )


async def test_the_local_descr_is_synced_after_the_class_tag_is_written(conn) -> None:
    """Иначе veto читает пустой descr и теряет только что купленный адрес.

    Пустая строка не разбирается грамматикой, планировщик трактует её как
    непродлеваемую — и прокси, за который мы заплатили минуту назад, не
    продлевается никогда.
    """
    fake = Fake(getprice={"status": "yes", "price": "7.70"}, buy=BOUGHT)
    res = await applier(conn, fake).apply([Command(Op.BUY, version=3, period_days=7)])
    assert res[0].ok
    row = conn.execute("SELECT descr FROM proxy WHERE p6_id = 11").fetchone()
    assert unpack(row["descr"]).renew == "R", f"локальный descr: {row['descr']!r}"
    assert len(row["descr"]) == 19
