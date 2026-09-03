"""``mkt`` — операционный инструмент.

Правило, из которого он вырос: **у каждого автоматического решения должен быть
ручной эквивалент.** Система, которая сама купила прокси, сама его списала и
не даёт человеку сделать то же руками, в три часа ночи неотлаживаема.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mktlink.budget import derive, plan, request_ladder
from mktlink.constants import MARKETPLACES
from mktlink.marketplaces.selectors import REGISTRY as SELECTORS
from mktlink.proxy6.descr import DescrError, is_ours, unpack
from mktlink.settings import Settings
from mktlink.store.db import connect, init_db, is_never_renew, set_never_renew, spent_kop_last_30d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mkt", description="Операции mktlink")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Создать или обновить схему")

    b = sub.add_parser("budget", help="Показать бюджет и лестницы")
    b.add_argument("--budget-ms", type=int, default=None)
    b.add_argument("--hops", type=int, default=0)

    p = sub.add_parser("proxy", help="Прокси")
    psub = p.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("ls", help="Список")
    cond = psub.add_parser("condemn", help="Пометить «никогда не продлевать»")
    cond.add_argument("p6_id", type=int)
    cond.add_argument("--reason", default="manual")
    sp = psub.add_parser("spend", help="Траты за 30 дней")
    sp.add_argument("--json", action="store_true")

    d = sub.add_parser("descr", help="Разобрать тэг")
    d.add_argument("value")

    s = sub.add_parser("selectors", help="Состояние запиненных путей")
    s.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    cfg = Settings()

    if args.cmd == "init-db":
        init_db(cfg.db_path)
        print(f"схема готова: {cfg.db_path}")
        return 0

    if args.cmd == "budget":
        return _budget(cfg, args.budget_ms or cfg.response_budget_ms, args.hops)

    if args.cmd == "descr":
        return _descr(args.value)

    if args.cmd == "selectors":
        return _selectors(args.json)

    if args.cmd == "proxy":
        conn = connect(cfg.db_path)
        try:
            if args.pcmd == "ls":
                return _proxy_ls(conn)
            if args.pcmd == "condemn":
                return _proxy_condemn(conn, args.p6_id, args.reason)
            if args.pcmd == "spend":
                return _proxy_spend(conn, args.json)
        finally:
            conn.close()

    return 1


def _budget(cfg: Settings, budget_ms: int, hops: int) -> int:
    print(f"RESPONSE_BUDGET_MS = {budget_ms}  (подсказка клиенту: "
          f"таймаут > {budget_ms + 500} мс)")
    for mp in MARKETPLACES:
        caps = derive(budget_ms, mp, hops)
        try:
            pl = plan(budget_ms, mp, hops)
        except ValueError as exc:
            print(f"\n{mp}: лестницы нет — {exc}")
            continue
        print(f"\n{mp}: NET {caps.net_ms} мс, резерв хвоста {caps.stage_reserve_ms} мс")
        for rung, cap in zip(pl.rungs, pl.caps, strict=True):
            print(f"    {rung.name:24} {rung.kind:7} cap {cap:5} (пол {rung.floor_ms})")
        if pl.guards:
            print(f"    {'гарды':24} {'':7} {sum(pl.guards):5} ({len(pl.guards)} × "
                  f"{pl.guards[0]})")
        print(f"    {'резидуал под спейсинг':24} {'':7} {pl.prewait_ms:5}")
        total = sum(pl.caps) + sum(pl.guards) + pl.prewait_ms
        mark = "ok" if total == caps.net_ms else "РАСХОЖДЕНИЕ"
        print(f"    сумма {total} = NET {caps.net_ms}  [{mark}]")
        skipped = [r.name for r in request_ladder(mp) if r not in pl.rungs]
        if skipped:
            print(f"    не влезли: {', '.join(skipped)}")
    return 0


def _descr(value: str) -> int:
    if not is_ours(value):
        print(f"{value!r}: не наш (namespace guard). Никогда не продлевается и не удаляется.")
        return 0
    try:
        d = unpack(value)
    except DescrError as exc:
        print(f"{value!r}: {exc}")
        print("Трактуется как списанный и непродлеваемый — безопасная сторона ошибки.")
        return 0
    print(json.dumps(
        {
            "marketplace": d.mk,
            "tier": d.tier,
            "proxy6_version": d.version,
            "role": d.role,
            "renew": d.renew,
            "never_renew": d.never_renew,
            "retired": d.retired,
            "gen": d.gen,
            "length": len(value),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def _selectors(as_json: bool) -> int:
    unpinned = SELECTORS.unpinned()
    if as_json:
        print(json.dumps({"unpinned": unpinned}, ensure_ascii=False))
        return 0
    if not unpinned:
        print("все пути запинены по живым payload'ам")
        return 0
    print("работают на гипотезах (продавец будет unknown_layout):")
    for mp in unpinned:
        print(f"    {mp}")
    print("\nзакрывается запуском scripts/pin_selectors по реальной карточке")
    return 0


def _proxy_ls(conn: Any) -> int:
    rows = conn.execute(
        "SELECT p6_id, ip, version, descr, state, never_renew, adopt_pending, term_end"
        " FROM proxy ORDER BY p6_id"
    ).fetchall()
    if not rows:
        print("прокси нет. Первый будет куплен при первом запросе.")
        return 0
    for r in rows:
        flags = []
        if r["never_renew"]:
            flags.append("НЕ ПРОДЛЕВАТЬ")
        if r["adopt_pending"]:
            flags.append("ждёт усыновления")
        print(
            f"{r['p6_id']:>8}  v{r['version']}  {r['ip']:<16} {r['state']:<12}"
            f" {r['descr']:<21} {' '.join(flags)}"
        )
    return 0


def _proxy_condemn(conn: Any, p6_id: int, reason: str) -> int:
    set_never_renew(conn, p6_id, reason)
    assert is_never_renew(conn, p6_id)
    print(f"{p6_id}: помечен «никогда не продлевать» ({reason}).")
    print("Метка двусторонняя: живой descr будет переписан в X при следующем setdescr.")
    return 0


def _proxy_spend(conn: Any, as_json: bool) -> int:
    kop = spent_kop_last_30d(conn)
    data = {"spent_rub": kop / 100, "cap_rub": 600.0, "headroom_rub": (60_000 - kop) / 100}
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"за 30 дней: {data['spent_rub']:.2f} ₽ из {data['cap_rub']:.0f} ₽"
              f" (осталось {data['headroom_rub']:.2f} ₽)")
        print("Считаются только buy и prolong: forfeit и discard — учёт уже потраченного.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
