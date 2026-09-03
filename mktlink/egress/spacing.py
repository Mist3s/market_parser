"""Минимальный интервал между исходящими на паре (маркетплейс, прокси).

Это всё, что осталось от двухуровневых GCRA-корзин прежней редакции, и
сокращение обосновано арифметикой, а не вкусом. При 2–3 запросах в минуту
средний зазор между клиентскими запросами — около 59 секунд, а самый строгий
интервал у нас 5 секунд. Между запросами гард не связывает никогда.

**Ключ — пара, а не прокси.** Общий на прокси ограничитель дал бы Я.Маркету
ozon'овские 5 секунд, и лестница из четырёх ступеней перестала бы
существовать: четыре ступени и три гарда по 5000 — это 15 секунд обязательного
ожидания внутри окна в 14 секунд. Партиционирование по маркетплейсам здесь
условие корректности, а не тонкая настройка.

**Гард самофинансируется — но только у Я.Маркета.** Инвариант
``MIN_INTERVAL[mp] <= min(floor ступеней)`` держится для ym (600 ≤ 600) и не
держится для Ozon и WB. Там, где он держится, ожидание всегда оплачено
временем, которое предыдущая ступень не потратила: если она отработала
успешно, она заняла не меньше своего пола, и ждать уже нечего. Связывает гард
только на быстрых отказах — то есть ровно тогда, когда замедлиться и надо.

Состояние переживает рестарт: иначе перезапуск процесса разрешил бы выстрел
сразу после предыдущего, и антибот увидел бы всплеск ровно в тот момент,
когда мы меньше всего этого хотим.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

from mktlink.constants import MIN_INTERVAL_MS
from mktlink.store.db import immediate
from mktlink.timing.deadline import Deadline, stage


@dataclass(frozen=True, slots=True)
class Reservation:
    """Сколько ждать до выпуска запроса."""

    mp: str
    proxy_id: int
    wait_ms: int

    @property
    def binds(self) -> bool:
        return self.wait_ms > 0


def reserve(
    conn: sqlite3.Connection, mp: str, proxy_id: int, *, now_ms: int | None = None
) -> Reservation:
    """Занять слот и узнать, сколько ждать.

    Read-modify-write под ``BEGIN IMMEDIATE``: два процесса, прочитавшие одну
    строку и оба решившие её обновить, иначе получат ошибку вместо очереди.

    Резервация происходит СРАЗУ, до ожидания. Это намеренно: если резервировать
    после сна, два одновременных вызывающих оба увидят свободный слот и оба
    выстрелят. Цена — при отмене запроса слот остаётся занятым, но при трёх
    запросах в минуту это дешевле, чем всплеск.
    """
    interval = MIN_INTERVAL_MS[mp]
    now = now_ms if now_ms is not None else int(time.monotonic() * 1000)
    with immediate(conn):
        row = conn.execute(
            "SELECT next_allowed_ms FROM spacing WHERE mp = ? AND proxy_id = ?",
            (mp, proxy_id),
        ).fetchone()
        next_allowed = int(row["next_allowed_ms"]) if row else 0
        depart = max(now, next_allowed)
        conn.execute(
            "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES (?, ?, ?)"
            " ON CONFLICT(mp, proxy_id) DO UPDATE SET next_allowed_ms = excluded.next_allowed_ms",
            (mp, proxy_id, depart + interval),
        )
    return Reservation(mp=mp, proxy_id=proxy_id, wait_ms=max(0, depart - now))


async def wait(dl: Deadline, res: Reservation, *, reserve_ms: int) -> None:
    """Отбыть зарезервированное ожидание внутри дедлайна.

    Ожидание — такая же стадия бюджета, как сетевой вызов: если оно не влезает
    в остаток, начинать запрос нельзя, и честный ответ — 202, а не молчаливое
    превышение потолка.
    """
    if not res.binds:
        return
    async with stage(dl, "spacing", cap_ms=res.wait_ms, reserve_ms=reserve_ms):
        await asyncio.sleep(res.wait_ms / 1000)


def peek(conn: sqlite3.Connection, mp: str, proxy_id: int, *, now_ms: int | None = None) -> int:
    """Сколько пришлось бы ждать, ничего не занимая.

    Нужно планировщику: решение «влезет ли ступень» принимается до резервации.
    """
    now = now_ms if now_ms is not None else int(time.monotonic() * 1000)
    row = conn.execute(
        "SELECT next_allowed_ms FROM spacing WHERE mp = ? AND proxy_id = ?", (mp, proxy_id)
    ).fetchone()
    if row is None:
        return 0
    return max(0, int(row["next_allowed_ms"]) - now)
