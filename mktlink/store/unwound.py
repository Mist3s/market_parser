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
from urllib.parse import urlsplit

from mktlink.urls.redirects import shortlink_key


def normalise(short_url: str) -> str:
    """Свести написания одной короткой ссылки к одному ключу.

    Без этого кэш не попадал в основном рабочем случае. ЗАМЕРЕНО на четырёх
    написаниях одной ссылки — ``ozon.ru/t/AbC123``,
    ``www.ozon.ru/t/AbC123``, тот же со слэшем на конце и тот же с
    ``?utm_source=tg``: четыре разных ключа, четыре раскрутки, четыре записи.
    А именно последнее написание и приходит чаще всего: ссылку пересылают из
    мессенджера, и он дописывает метку.

    Что отбрасывается и почему это безопасно:

    * **query и фрагмент целиком.** У обеих наших коротких форм код лежит в
      ПУТИ (``/t/<code>`` у Ozon, ``/cc/<code>`` у Я.Маркета — см.
      :mod:`mktlink.urls.registry`), поэтому никакой параметр не может
      изменить, куда ссылка ведёт. Всё, что в query, — трекинг.
    * **префикс ``www.`` и регистр хоста.** Хост нечувствителен к регистру
      по стандарту, а ``www`` у обоих маркетплейсов ведёт туда же.
    * **слэш в конце пути.** Тот же ресурс.

    Схема (``https``) не отбрасывается: валидатор всё равно не пропускает
    ничего другого, и подменять её здесь значило бы прятать это правило в
    неожиданном месте.
    """
    parts = urlsplit(short_url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{host}{path}"


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
            (shortlink_key(normalise(short_url)),),
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
            (shortlink_key(normalise(short_url)), canonical, hops),
        )
        self.conn.commit()


__all__ = ["Unwound", "UnwoundLinks", "normalise"]
