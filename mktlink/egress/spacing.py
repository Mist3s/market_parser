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

**Часы — настенные, а не монотонные.** Момент лежит в SQLite и читается
другими процессами и после перезапуска, поэтому шкала обязана быть общей и
переживать перезагрузку. ``time.monotonic()`` прежней редакции отсчитывался
от загрузки машины: после перезагрузки хоста записанный до неё момент
оказывался впереди на весь прежний аптайм, и пара получала отказ на каждый
запрос, пока новый аптайм не догонит старый. Настенные часы за это платят
прыжками назад при синхронизации времени; от них защищает потолок долга, см.
:func:`_trusted`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

from mktlink.constants import MIN_INTERVAL_MS, RESPONSE_BUDGET_MAX_MS
from mktlink.store.db import immediate
from mktlink.timing.deadline import Deadline, DeadlineExceeded


@dataclass(frozen=True, slots=True)
class Reservation:
    """Сколько ждать до выпуска запроса."""

    mp: str
    proxy_id: int
    wait_ms: int
    #: Ожидание длиннее допустимого, и слот НЕ занят: запрос не пойдёт.
    refused: bool = False

    @property
    def binds(self) -> bool:
        return self.wait_ms > 0


def reserve(
    conn: sqlite3.Connection,
    mp: str,
    proxy_id: int,
    *,
    now_ms: int | None = None,
    max_wait_ms: int | None = None,
) -> Reservation:
    """Занять слот и узнать, сколько ждать.

    Read-modify-write под ``BEGIN IMMEDIATE``: два процесса, прочитавшие одну
    строку и оба решившие её обновить, иначе получат ошибку вместо очереди.

    Резервация происходит СРАЗУ, до ожидания. Это намеренно: если резервировать
    после сна, два одновременных вызывающих оба увидят свободный слот и оба
    выстрелят. Цена — при отмене запроса слот остаётся занятым, но при трёх
    запросах в минуту это дешевле, чем всплеск.

    ``max_wait_ms`` — сколько вызывающий готов ждать. Дольше — отказ, и слот
    при отказе НЕ занимается: запрос, который не пойдёт, не вправе отодвигать
    следующий. Прежняя редакция занимала его и при отказе, поэтому серия
    отказов копила долг, и каждый следующий запрос ждал дольше предыдущего.
    Решение принимается под той же блокировкой, что и резервация: между
    «посмотреть» и «занять» слот успел бы занять другой процесс.
    """
    interval = MIN_INTERVAL_MS[mp]
    now = now_ms if now_ms is not None else _wall_ms()
    with immediate(conn):
        row = conn.execute(
            "SELECT next_allowed_ms FROM spacing WHERE mp = ? AND proxy_id = ?",
            (mp, proxy_id),
        ).fetchone()
        next_allowed = _trusted(int(row["next_allowed_ms"]), now, interval) if row else 0
        depart = max(now, next_allowed)
        wait_ms = depart - now
        if max_wait_ms is not None and wait_ms > max_wait_ms:
            return Reservation(mp=mp, proxy_id=proxy_id, wait_ms=wait_ms, refused=True)
        conn.execute(
            "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES (?, ?, ?)"
            " ON CONFLICT(mp, proxy_id) DO UPDATE SET next_allowed_ms = excluded.next_allowed_ms",
            (mp, proxy_id, depart + interval),
        )
    return Reservation(mp=mp, proxy_id=proxy_id, wait_ms=wait_ms)


async def wait(dl: Deadline, res: Reservation, *, reserve_ms: int) -> None:
    """Отбыть зарезервированное ожидание внутри дедлайна.

    Ожидание — такая же стадия бюджета, как сетевой вызов: если оно не влезает
    в остаток, начинать запрос нельзя, и честный ответ — 202, а не молчаливое
    превышение потолка. Отказ — сразу, а не после сна до края бюджета.

    Таймера здесь нет намеренно, хотя у сетевых стадий он есть: длительность
    сна известна заранее и уже проверена на остаток. ЗАМЕР 2026-10-01: прежняя
    редакция спала ``wait_ms`` внутри ``stage(cap_ms=wait_ms)``, таймер стадии
    срабатывал раньше сна, и каждое ожидание, которое влезало в бюджет,
    засчитывалось как превышение — 20 из 20 при запасе в 30 секунд.
    """
    if not res.binds:
        return
    if not dl.afford(res.wait_ms, reserve_ms):
        raise DeadlineExceeded("spacing", ledger=dl.spent)
    started = time.monotonic()
    try:
        await asyncio.sleep(res.wait_ms / 1000)
    finally:
        dl.record("spacing", int((time.monotonic() - started) * 1000))


def peek(conn: sqlite3.Connection, mp: str, proxy_id: int, *, now_ms: int | None = None) -> int:
    """Сколько пришлось бы ждать, ничего не занимая.

    Нужно планировщику: решение «влезет ли ступень» принимается до резервации.
    """
    now = now_ms if now_ms is not None else _wall_ms()
    row = conn.execute(
        "SELECT next_allowed_ms FROM spacing WHERE mp = ? AND proxy_id = ?", (mp, proxy_id)
    ).fetchone()
    if row is None:
        return 0
    return max(0, _trusted(int(row["next_allowed_ms"]), now, MIN_INTERVAL_MS[mp]) - now)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _trusted(next_allowed_ms: int, now_ms: int, interval_ms: int) -> int:
    """Записанный момент — или ``now_ms``, если такого долга не мог оставить никто.

    Допущенный запрос ждёт меньше бюджета ответа и занимает слот на
    ``interval_ms`` после выхода, поэтому честный долг не длиннее
    ``RESPONSE_BUDGET_MAX_MS + interval_ms``. Длиннее — значит, часы прыгнули
    назад или строку писали другие часы. Верить такой строке значило бы
    отказывать паре, пока часы её не догонят; сбросить её стоит одного
    выстрела раньше интервала.
    """
    if next_allowed_ms - now_ms > RESPONSE_BUDGET_MAX_MS + interval_ms:
        return now_ms
    return next_allowed_ms
