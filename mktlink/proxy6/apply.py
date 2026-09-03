"""Исполнитель команд планировщика. ЕДИНСТВЕННЫЙ, кто зовёт клиент proxy6.

Планировщик решает, здесь исполняется. Разделение не церемониальное: решение
о трате — чистая функция, которую можно проверить тестом, а исполнение —
грязное и неповторимое, и его надо держать в одном месте, где видно все
последствия.

Три правила исполнения:

1. **Заявка на трату записывается ДО вызова.** Иначе потерянный ответ — это
   трата, о которой мы не знаем, и денежный потолок считает не то.
2. **``buy`` не повторяется никогда.** Потерянный ответ разбирается по nonce
   в ``descr``: ``getproxy(descr=<nonce>)`` показывает, состоялась ли покупка.
   Обратное неверно и это важно — пустой ответ НЕ доказывает, что покупки не
   было: она может быть ещё в полёте, поэтому nonce не переиспользуется сразу.
3. **Метка never-renew коммитится ОТДЕЛЬНОЙ транзакцией, без денег.**
   Денежный потолок не имеет права откатить именно эту запись: иначе защита
   от перерасхода уничтожает защиту от продления сожжённого адреса.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from mktlink.proxy6.client import Proxy6Client
from mktlink.proxy6.descr import (
    NONCE_ALPHABET,
    DescrError,
    is_deletable,
    pack_birth,
    pack_nonce,
    pack_write,
    unpack,
)
from mktlink.proxy6.errors import Proxy6InsufficientBalance, Proxy6NotFound
from mktlink.proxy6.plan import Command, Op
from mktlink.store.db import SpendRejected, immediate, record_spend, set_never_renew


@dataclass(frozen=True, slots=True)
class Applied:
    command: Command
    ok: bool
    detail: str = ""


class Applier:
    def __init__(self, client: Proxy6Client, conn: sqlite3.Connection, *, gen: int = 1) -> None:
        self._p6 = client
        self._conn = conn
        self._gen = gen

    async def apply(self, cmds: list[Command]) -> list[Applied]:
        out: list[Applied] = []
        for cmd in cmds:
            try:
                out.append(await self._one(cmd))
            except Proxy6InsufficientBalance:
                # Единственная ошибка, которая замораживает И закупку, И
                # продление. Частичное продление хуже полного отказа: парк
                # начинает истекать неравномерно и становится непредсказуемым.
                out.append(Applied(cmd, False, "insufficient balance — everything frozen"))
                break
            except (SpendRejected, DescrError, Proxy6NotFound, sqlite3.IntegrityError) as exc:
                # Опечатка в цене или дубль проваливают ОДНУ команду, а не
                # весь проход: остальные решения планировщика всё ещё верны.
                out.append(Applied(cmd, False, str(exc)))
        return out

    async def _one(self, cmd: Command) -> Applied:
        if cmd.op is Op.BUY:
            return await self._buy(cmd)
        if cmd.op is Op.PROLONG:
            return await self._prolong(cmd)
        if cmd.op is Op.MARK_NEVER_RENEW:
            return self._condemn(cmd)
        if cmd.op is Op.SETDESCR:
            return await self._setdescr(cmd)
        if cmd.op is Op.DELETE:
            return await self._delete(cmd)
        if cmd.op is Op.ADOPT_PENDING:
            return self._adopt_pending(cmd)
        if cmd.op is Op.NOTE_FOREIGN:
            return self._note_foreign(cmd)
        return Applied(cmd, False, f"unknown op {cmd.op}")

    # --- деньги ---------------------------------------------------------------

    async def _buy(self, cmd: Command) -> Applied:
        version = cmd.version or 3
        period = cmd.period_days or 7
        nonce = pack_nonce(datetime.now(UTC).date(), _rand6())

        quote = await self._p6.getprice(count=1, period=period, version=version)
        kop = int(round(float(quote.get("price", 0)) * 100))

        # Заявка ДО вызова: потерянный ответ обязан быть виден потолку.
        with immediate(self._conn):
            self._conn.execute(
                "INSERT INTO proxy_order (nonce, version, period_days, status)"
                " VALUES (?, ?, ?, 'intent')",
                (nonce, version, period),
            )
            record_spend(
                self._conn,
                kind="buy",
                status="intent",
                kop=kop,
                version=version,
                period_days=period,
                nonce=nonce,
            )

        # Повторов нет и быть не может: повтор здесь стоит денег.
        result = await self._p6.buy(
            count=1, period=period, version=version, descr=nonce
        )

        rows = result.get("list") or {}
        items = list(rows.values()) if isinstance(rows, dict) else list(rows)
        if not items:
            return Applied(cmd, False, f"buy returned nothing; reconcile by nonce {nonce}")

        p6_id = int(items[0]["id"])
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE proxy_order SET status = 'landed', p6_id = ?, settled_at = unixepoch()"
                " WHERE nonce = ?",
                (p6_id, nonce),
            )
            self._conn.execute(
                "UPDATE proxy_spend SET status = 'confirmed', p6_id = ? WHERE nonce = ?",
                (p6_id, nonce),
            )
            self._upsert_proxy(items[0], version)

        # Классовый тэг вместо nonce. Единственное место, где рождается 'R'.
        await self._p6.setdescr(new=pack_birth(version, self._gen), ids=[p6_id])
        return Applied(cmd, True, f"bought {p6_id}")

    async def _prolong(self, cmd: Command) -> Applied:
        assert cmd.p6_id is not None
        row = self._conn.execute(
            "SELECT term_end, never_renew FROM proxy WHERE p6_id = ?", (cmd.p6_id,)
        ).fetchone()
        if row is None:
            return Applied(cmd, False, "unknown proxy")
        if row["never_renew"]:
            # Двойная страховка: планировщик это уже проверил, но продление —
            # необратимая трата, и вторая проверка здесь дешевле ошибки.
            return Applied(cmd, False, "never_renew is set")

        period = cmd.period_days or 7
        witness = int(row["term_end"])

        # Свидетель — срок ДО операции. Уникальный индекс отвергнет дубль
        # силами самой БД, а не нашей внимательностью.
        try:
            with immediate(self._conn):
                self._conn.execute(
                    "INSERT INTO proxy_renewal (p6_id, term_end_before, period_days, status)"
                    " VALUES (?, ?, ?, 'intent')",
                    (cmd.p6_id, witness, period),
                )
                record_spend(
                    self._conn,
                    kind="prolong",
                    status="intent",
                    kop=0,
                    version=3,
                    period_days=period,
                    p6_id=cmd.p6_id,
                )
        except sqlite3.IntegrityError:
            return Applied(cmd, False, "already prolonged at this term_end")

        await self._p6.prolong(period=period, ids=[cmd.p6_id])
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE proxy_renewal SET status = 'confirmed'"
                " WHERE p6_id = ? AND term_end_before = ?",
                (cmd.p6_id, witness),
            )
        return Applied(cmd, True, "prolonged")

    # --- метки ------------------------------------------------------------------

    def _condemn(self, cmd: Command) -> Applied:
        """Отдельная транзакция БЕЗ денег.

        Денежный потолок не имеет права откатить эту запись: иначе защита от
        перерасхода уничтожает защиту от продления сожжённого адреса.
        """
        assert cmd.p6_id is not None
        set_never_renew(self._conn, cmd.p6_id, cmd.reason or "condemned")
        return Applied(cmd, True, "never_renew set")

    async def _setdescr(self, cmd: Command) -> Applied:
        assert cmd.p6_id is not None
        row = self._conn.execute(
            "SELECT descr, never_renew FROM proxy WHERE p6_id = ?", (cmd.p6_id,)
        ).fetchone()
        if row is None:
            return Applied(cmd, False, "unknown proxy")

        if cmd.descr == "adopt-orphan":
            new = pack_birth(3, self._gen)
        elif cmd.descr == "retire":
            cur = unpack(row["descr"])
            new = pack_write(cur, role="r", local_veto=True)
        else:
            new = cmd.descr or row["descr"]

        await self._p6.setdescr(new=new, ids=[cmd.p6_id])
        self._conn.execute(
            "UPDATE proxy SET descr = ? WHERE p6_id = ?", (new, cmd.p6_id)
        )
        return Applied(cmd, True, new)

    async def _delete(self, cmd: Command) -> Applied:
        assert cmd.p6_id is not None
        row = self._conn.execute(
            "SELECT descr, term_end FROM proxy WHERE p6_id = ?", (cmd.p6_id,)
        ).fetchone()
        if row is None:
            return Applied(cmd, False, "unknown proxy")
        if not is_deletable(row["descr"]):
            # Регулярка защищает от оператора и от нас самих: удаление не
            # списанного адреса необратимо и ничего не покупает.
            return Applied(cmd, False, f"refusing delete on {row['descr']}")
        await self._p6.delete(ids=[cmd.p6_id])
        self._conn.execute("DELETE FROM proxy WHERE p6_id = ?", (cmd.p6_id,))
        return Applied(cmd, True, "deleted")

    def _adopt_pending(self, cmd: Command) -> Applied:
        assert cmd.p6_id is not None
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE proxy SET never_renew = 1, adopt_pending = 1 WHERE p6_id = ?",
                (cmd.p6_id,),
            )
        return Applied(cmd, True, "awaiting manual adoption")

    def _note_foreign(self, cmd: Command) -> Applied:
        assert cmd.p6_id is not None
        self._conn.execute(
            "INSERT INTO foreign_ack (p6_id, descr) VALUES (?, ?)"
            " ON CONFLICT(p6_id) DO UPDATE SET descr = excluded.descr",
            (cmd.p6_id, cmd.descr or ""),
        )
        return Applied(cmd, True, "counted, never touched")

    # --- внутреннее ----------------------------------------------------------------

    def _upsert_proxy(self, row: dict, version: int) -> None:
        self._conn.execute(
            "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state,"
            " term_end, never_renew) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unverified', ?, 0)"
            " ON CONFLICT(p6_id) DO UPDATE SET term_end = excluded.term_end",
            (
                int(row["id"]),
                str(row.get("ip", "")),
                str(row.get("host", "")),
                int(row.get("port", 0)),
                str(row.get("user", "")),
                str(row.get("pass", "")),
                version,
                str(row.get("descr", "")),
                int(row.get("unixtime_end", 0)),
            ),
        )


def _rand6() -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(6))
