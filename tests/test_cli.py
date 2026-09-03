"""CLI: у каждого автоматического решения есть ручной эквивалент."""

from __future__ import annotations

import json

import pytest

from mktlink.cli import main
from mktlink.store.db import connect, init_db, is_never_renew


def test_budget_reproduces_the_ledger_identity(capsys) -> None:
    assert main(["budget"]) == 0
    out = capsys.readouterr().out
    assert "РАСХОЖДЕНИЕ" not in out, "тождество обязано сходиться для всех трёх"
    assert out.count("[ok]") == 3


def test_budget_shows_what_did_not_fit(capsys) -> None:
    """Честность важнее красоты: молча урезанная лестница читается как полная."""
    main(["budget", "--budget-ms", "5000"])
    out = capsys.readouterr().out
    assert "не влезли" in out


def test_descr_decodes_our_tag(capsys) -> None:
    assert main(["descr", "mp1.oz.s.r.X.ru.g07"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["never_renew"] is True and data["retired"] is True
    assert data["length"] == 19


def test_descr_refuses_to_interpret_a_foreign_tag(capsys) -> None:
    main(["descr", "someones-proxy"])
    assert "не наш" in capsys.readouterr().out


def test_descr_treats_garbage_as_never_renew(capsys) -> None:
    main(["descr", "mp1.GARBAGE"])
    assert "непродлеваемый" in capsys.readouterr().out


def test_selectors_names_who_is_still_guessing(capsys) -> None:
    main(["selectors", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert set(data["unpinned"]) == {"ozon", "wb", "ym"}


def test_condemn_is_available_by_hand(tmp_path, monkeypatch, capsys) -> None:
    """Система, списавшая адрес сама, обязана позволить сделать это и руками."""
    db = tmp_path / "m.sqlite"
    monkeypatch.setenv("MKTLINK_DB_PATH", str(db))
    init_db(db)
    assert main(["proxy", "condemn", "42", "--reason", "manual test"]) == 0
    assert "42" in capsys.readouterr().out

    conn = connect(db)
    try:
        assert is_never_renew(conn, 42)
    finally:
        conn.close()


def test_spend_reports_against_the_cap(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "m.sqlite"
    monkeypatch.setenv("MKTLINK_DB_PATH", str(db))
    init_db(db)
    main(["proxy", "spend", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["cap_rub"] == 600.0
    assert data["headroom_rub"] == 600.0


def test_empty_pool_says_so_plainly(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "m.sqlite"
    monkeypatch.setenv("MKTLINK_DB_PATH", str(db))
    init_db(db)
    main(["proxy", "ls"])
    assert "будет куплен при первом запросе" in capsys.readouterr().out


def test_unknown_command_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        main(["nonsense"])
