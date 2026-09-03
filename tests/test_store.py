"""Схема и денежные потолки.

Потолки проверяются исполнением DDL, а не чтением: триггер, который «должен
срабатывать», и триггер, который срабатывает, — разные вещи, и разница здесь
измеряется в рублях.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from mktlink.store.db import (
    SpendRejected,
    immediate,
    init_db,
    is_never_renew,
    record_spend,
    set_never_renew,
    spent_kop_last_30d,
)
from mktlink.store.db import connect as db_connect


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "mktlink.sqlite"
    init_db(path)
    c = db_connect(path)
    yield c
    c.close()


def test_schema_creates_eighteen_tables(conn) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names = sorted(r["name"] for r in rows)
    assert names == sorted(
        [
            "api_key",
            "foreign_ack",
            "jar",
            "key_usage",
            "marketplace_policy",
            "never_prolong",
            "product",
            "proxy",
            "proxy_attempt",
            "proxy_health",
            "proxy_order",
            "proxy_renewal",
            "proxy_spend",
            "rejections",
            "selectors",
            "shortlink",
            "spacing",
            "request_log",
        ]
    )
    assert len(names) == 18


def test_init_db_is_idempotent(tmp_path) -> None:
    path = tmp_path / "x.sqlite"
    init_db(path)
    init_db(path)
    c = db_connect(path)
    try:
        assert c.execute("SELECT COUNT(*) AS n FROM proxy").fetchone()["n"] == 0
    finally:
        c.close()


def test_wal_and_foreign_keys_are_on(conn) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def _age_rows(conn, sql: str, params: tuple = ()) -> None:
    """Вставить строку задним числом, временно сняв триггер.

    Обычным путём это невозможно, и это правильно: ``TS_SKEW`` не даёт
    приложению назвать время траты, иначе часы или битый вызывающий обходят
    ``MIN_BUY_GAP`` и скользящие окна. Побочное следствие, названное в
    спецификации: построчное восстановление леджера из бэкапа тоже
    невозможно — бэкап делается копией файла (``VACUUM INTO``), а не INSERT'ами.
    """
    conn.execute("DROP TRIGGER proxy_spend_caps_ins")
    try:
        conn.execute(sql, params)
    finally:
        conn.executescript(
            (
                pathlib.Path(__file__).resolve().parents[1]
                / "mktlink"
                / "store"
                / "schema.sql"
            ).read_text(encoding="utf-8")
        )


# --- деньги ------------------------------------------------------------------


def test_a_normal_buy_is_recorded(conn) -> None:
    rid = record_spend(
        conn, kind="buy", status="confirmed", kop=870, version=3, period_days=7
    )
    assert rid > 0
    assert spent_kop_last_30d(conn) == 870


def test_price_sanity_check_catches_a_typo_before_it_becomes_spend(conn) -> None:
    """1.40 ₽/сут для v3: 7 дней -> максимум 980 копеек, 981 уже опечатка."""
    record_spend(conn, kind="buy", status="confirmed", kop=980, version=3, period_days=7)
    with pytest.raises(sqlite3.IntegrityError):
        # prolong, а не buy: иначе первым сработает MIN_BUY_GAP и мы проверим
        # не тот потолок.
        record_spend(conn, kind="prolong", status="confirmed", kop=981, version=3, period_days=7)
    # 400 коп/сут для v4 — другой потолок той же проверки.
    record_spend(conn, kind="prolong", status="confirmed", kop=2800, version=4, period_days=7)


def test_min_buy_gap_blocks_a_second_buy_immediately_after(conn) -> None:
    record_spend(conn, kind="buy", status="confirmed", kop=100, version=3, period_days=7)
    with pytest.raises(SpendRejected) as ei:
        record_spend(conn, kind="buy", status="confirmed", kop=100, version=3, period_days=7)
    assert ei.value.cap == "MIN_BUY_GAP"


def test_hourly_buy_cap_blocks_the_fourth_not_the_third(conn) -> None:
    """Off-by-one здесь стоил бы блокировки законной замены.

    Триггер отклоняет, когда в окне УЖЕ >= 3 незанулённых закупки. Значит две
    застрявшие заявки оставляют третью возможной, а блокируют четвёртую.
    """
    for i in range(2):
        _age_rows(
            conn,
            "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
            " VALUES (unixepoch() - ?, 'buy', 'confirmed', 100, 3, 7)",
            (1000 + i * 10,),
        )
    # Третья проходит: в окне пока две.
    assert record_spend(conn, kind="buy", status="confirmed", kop=100, version=3, period_days=7) > 0

    # Четвёртая — нет. Состарим третью, чтобы снять MIN_BUY_GAP и проверить
    # именно часовой потолок.
    conn.execute("DELETE FROM proxy_spend WHERE ts > unixepoch() - 10")
    for i in range(3):
        _age_rows(
            conn,
            "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
            " VALUES (unixepoch() - ?, 'buy', 'confirmed', 100, 3, 7)",
            (1000 + i * 10,),
        )
    with pytest.raises(SpendRejected) as ei:
        record_spend(conn, kind="buy", status="confirmed", kop=100, version=3, period_days=7)
    assert ei.value.cap == "CAP_BUYS_HOURLY"


def test_monthly_rouble_cap_is_enforced_by_the_database(conn) -> None:
    """600 ₽ = 60 000 копеек. Обойти это из приложения нельзя."""
    _age_rows(
        conn,
        "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
        " VALUES (unixepoch() - 100, 'buy', 'confirmed', 59500, 4, 150)",
    )
    with pytest.raises(SpendRejected) as ei:
        record_spend(conn, kind="prolong", status="confirmed", kop=600, version=3, period_days=7)
    assert ei.value.cap == "CAP_RUB_MONTHLY"
    # А то, что влезает, проходит.
    assert record_spend(
        conn, kind="prolong", status="confirmed", kop=400, version=3, period_days=7
    ) > 0


def test_forfeit_cannot_trip_the_money_cap(conn) -> None:
    """Ключевое: списание сгоревших дней НЕ ИМЕЕТ ПРАВА откатить транзакцию.

    Иначе денежный потолок уничтожает запись never_renew, ради которой он и
    существует. Восемьдесят forfeit-строк по 8 ₽ = 640 ₽ — выше потолка 600 —
    и все проходят, потому что потолок их не видит.
    """
    for _ in range(80):
        conn.execute(
            "INSERT INTO proxy_spend (kind, status, kop, version, period_days)"
            " VALUES ('forfeit', 'confirmed', 800, 3, 7)"
        )
    total = conn.execute("SELECT SUM(kop) AS s FROM proxy_spend").fetchone()["s"]
    assert total == 64000, "640 ₽ учтено"
    assert spent_kop_last_30d(conn) == 0, "но потолок их не видит — это не наша новая трата"


def test_ts_skew_is_refused_so_caps_cannot_be_backdated(conn) -> None:
    """Приложение не имеет права назвать время траты."""
    with pytest.raises(sqlite3.IntegrityError, match="TS_SKEW"):
        conn.execute(
            "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
            " VALUES (unixepoch() - 100000, 'buy', 'confirmed', 100, 3, 7)"
        )
    # И вперёд тоже нельзя: часы, ушедшие вперёд, обошли бы скользящее окно.
    with pytest.raises(sqlite3.IntegrityError, match="TS_SKEW"):
        conn.execute(
            "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
            " VALUES (unixepoch() + 100000, 'buy', 'confirmed', 100, 3, 7)"
        )


def test_raising_a_confirmed_price_is_still_capped(conn) -> None:
    """Подтверждение по реальной котировке не проносит трату мимо потолка."""
    _age_rows(
        conn,
        "INSERT INTO proxy_spend (ts, kind, status, kop, version, period_days)"
        " VALUES (unixepoch() - 100, 'buy', 'confirmed', 59000, 4, 150)",
    )
    row = conn.execute("SELECT id FROM proxy_spend").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="CAP_RUB_MONTHLY"):
        conn.execute("UPDATE proxy_spend SET kop = 60500 WHERE id = ?", (row["id"],))


def test_lowering_a_price_is_always_allowed(conn) -> None:
    conn.execute(
        "INSERT INTO proxy_spend (kind, status, kop, version, period_days)"
        " VALUES ('buy', 'confirmed', 900, 3, 7)"
    )
    row = conn.execute("SELECT id FROM proxy_spend").fetchone()
    conn.execute("UPDATE proxy_spend SET kop = 870 WHERE id = ?", (row["id"],))
    assert spent_kop_last_30d(conn) == 870


# --- never-renew -------------------------------------------------------------


def test_never_renew_survives_and_is_two_sided(conn) -> None:
    conn.execute(
        "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state, term_end)"
        " VALUES (7, '1.2.3.4', 'h', 8000, 'u', 'p', 3, 'mp1.mp.s.a.R.ru.g01', 'active',"
        " unixepoch() + 86400)"
    )
    assert not is_never_renew(conn, 7)
    set_never_renew(conn, 7, "captcha_rate")
    assert is_never_renew(conn, 7)

    # Даже если строку proxy потерять целиком, запись never_prolong остаётся.
    conn.execute("DELETE FROM proxy WHERE p6_id = 7")
    assert is_never_renew(conn, 7)


def test_adopt_pending_requires_never_renew(conn) -> None:
    """Инверсия дефолта: найденный без локальной строки не благословляется."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state,"
            " term_end, never_renew, adopt_pending)"
            " VALUES (9, '1.2.3.4', 'h', 8000, 'u', 'p', 3, 'd', 'active', 1, 0, 1)"
        )


def test_renewal_witness_rejects_a_duplicate_prolong(conn) -> None:
    """Дубль отвергается самой БД: term_end до операции служит свидетелем."""
    conn.execute(
        "INSERT INTO proxy_renewal (p6_id, term_end_before, period_days, status)"
        " VALUES (5, 1700000000, 7, 'confirmed')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proxy_renewal (p6_id, term_end_before, period_days, status)"
            " VALUES (5, 1700000000, 7, 'intent')"
        )
    # Провалившаяся попытка свидетеля не занимает — повтор разрешён.
    conn.execute("UPDATE proxy_renewal SET status = 'failed' WHERE p6_id = 5")
    conn.execute(
        "INSERT INTO proxy_renewal (p6_id, term_end_before, period_days, status)"
        " VALUES (5, 1700000000, 7, 'intent')"
    )


def test_immediate_rolls_back_on_error(conn) -> None:
    with pytest.raises(RuntimeError):
        with immediate(conn):
            conn.execute("INSERT INTO rejections (code) VALUES ('x')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) AS n FROM rejections").fetchone()["n"] == 0


def test_health_is_keyed_by_proxy_and_marketplace(conn) -> None:
    """Один IP — три независимые репутации, потому что они действительно независимы."""
    for mp in ("ozon", "wb", "ym"):
        conn.execute(
            "INSERT INTO proxy_health (p6_id, mp, ok_n, bad_n) VALUES (1, ?, 0, 0)", (mp,)
        )
    conn.execute("UPDATE proxy_health SET bad_n = 40 WHERE p6_id = 1 AND mp = 'ozon'")
    rows = {r["mp"]: r["bad_n"] for r in conn.execute("SELECT mp, bad_n FROM proxy_health")}
    assert rows == {"ozon": 40, "wb": 0, "ym": 0}


def test_egress_attribution_is_constrained(conn) -> None:
    for eg in ("proxy", "direct", "forge"):
        conn.execute(
            "INSERT INTO proxy_attempt (mp, verdict, egress) VALUES ('ym', 'OK', ?)", (eg,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proxy_attempt (mp, verdict, egress) VALUES ('ym', 'OK', 'guess')"
        )


def test_a_connection_survives_use_from_another_thread(tmp_path) -> None:
    """Дефект, который проявляется только под настоящим ASGI-сервером.

    Соединение, привязанное к потоку создания, падает на первом же запросе,
    потому что сервер обслуживает в потоках пула. Тесты чистых функций этого
    не видят вовсе.
    """
    import sqlite3
    import threading

    init_db(tmp_path / "t.sqlite")
    c = db_connect(tmp_path / "t.sqlite")
    assert sqlite3.threadsafety == 3, "иначе cross-thread небезопасен и код обязан отказать"

    result: list[object] = []

    def worker() -> None:
        try:
            c.execute("INSERT INTO rejections (code) VALUES ('x')")
            result.append(c.execute("SELECT COUNT(*) AS n FROM rejections").fetchone()["n"])
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    c.close()
    assert result == [1], result
