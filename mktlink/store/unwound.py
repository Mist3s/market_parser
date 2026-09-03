"""Кэш раскрутки коротких ссылок. Таблица ``shortlink``.

Таблица существовала со дня написания схемы и **не использовалась ничем**:
``shortlink_key`` в :mod:`mktlink.urls.redirects` был написан и тоже никем не
вызывался. То есть раскрутка платилась заново при каждом запросе одной и той
же короткой ссылки — до трёх сетевых хопов по 550 мс из бюджета, при том что
``shortlink_key`` в своей же докстроке говорит: «короткие ссылки неизменяемы,
поэтому кэшируются надолго».

Почему это заметно только сейчас. Раньше раскрутка стоила времени, а время
было единственным ограничением. С появлением скрейпинг-API у запроса
появилась вторая цена — кредиты, — и трата бюджета на повторную раскрутку
стала отъедать окно у той ступени, которая эти кредиты и тратит.

Срока жизни у записи нет намеренно: короткая ссылка маркетплейса указывает на
один и тот же товар всё время своего существования. Истекать здесь нечему.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from mktlink.urls.redirects import shortlink_key


@dataclass(frozen=True, slots=True)
class Unwound:
    """Результат раскрутки: куда ведёт и сколько хопов это стоило."""

    canonical: str
    hops: int


@dataclass(slots=True)
class UnwoundLinks:
    """Отображение «короткая ссылка -> каноническая»."""

    conn: sqlite3.Connection

    def get(self, short_url: str) -> Unwound | None:
        row = self.conn.execute(
            "SELECT canonical, hops FROM shortlink WHERE short_sha256 = ?",
            (shortlink_key(short_url),),
        ).fetchone()
        if row is None:
            return None
        return Unwound(canonical=str(row["canonical"]), hops=int(row["hops"]))

    def put(self, short_url: str, canonical: str, hops: int) -> None:
        self.conn.execute(
            "INSERT INTO shortlink (short_sha256, canonical, hops) VALUES (?, ?, ?)"
            " ON CONFLICT(short_sha256) DO UPDATE SET"
            " canonical = excluded.canonical,"
            " hops = excluded.hops,"
            " resolved_at = unixepoch()",
            (shortlink_key(short_url), canonical, hops),
        )
        self.conn.commit()


__all__ = ["Unwound", "UnwoundLinks"]
