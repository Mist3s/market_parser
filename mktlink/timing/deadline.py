"""Один абсолютный дедлайн на запрос, из которого выводится каждый I/O-таймаут.

Механизм не зависит от значения ручки: он одинаково работает при 5 и при 15
секундах, потому что каждая стадия просит не «свои N мс», а срез от ОСТАТКА.
Ни одного фиксированного литерала таймаута внутри пакета нет — CI это проверяет.

Четыре правила, каждое добыто дефектом:

1. **Дедлайн принуждается на стороне ожидающего, а не внутри листа.** Отсюда
   межпроцессная граница ожидания минтинга: ``api`` ждёт сокет, ``forge``
   держит браузер, и пробить эту границу нельзя физически.
2. **Леджер пишется в ``finally``, а не на happy path.** Иначе стадия, съевшая
   бюджет и упавшая, в леджере не появляется, и постмортем показывает
   исчезнувшие секунды.
3. **``reserve_ms`` включает всё, что идёт ПОСЛЕ стадии**, а не только хвост:
   для ступени лестницы это ``STAGE_RESERVE`` плюс полы оставшихся допущенных
   ступеней с их гардами. Отсюда автоматическая деградация профиля: запрос,
   простоявший в очереди, просто получает меньше ступеней.
4. **После истечения ``APP_MS`` не выполняется ни одного I/O.** Между
   ``APP = B − 400`` и ``EDGE_READ = B − 100`` ровно 300 мс при любой ручке,
   поэтому лестница деградации читает ETA из памяти процесса, а не из сокета.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

_UNBOUNDED = 1 << 30

#: Установлен только внутри запроса. Единственный способ отличить горячий путь
#: от фонового — и потому единственный работающий гард против запуска браузера
#: или вызова proxy6 из обработчика.
_deadline: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "mktlink_deadline", default=None
)


class DeadlineExceeded(Exception):
    """Бюджет исчерпан до того, как стадия успела начаться или закончиться."""

    def __init__(self, stage_name: str, *, ledger: list[tuple[str, int]] | None = None) -> None:
        self.stage_name = stage_name
        self.ledger = list(ledger or ())
        super().__init__(f"deadline exceeded at stage {stage_name!r}")


class OffRequestPathViolation(RuntimeError):
    """Операция, недопустимая внутри запроса, была вызвана из запроса."""


@dataclass(slots=True)
class Deadline:
    """Абсолютный момент, после которого ответ обязан быть уже отдан."""

    at_monotonic: float
    trace_id: str
    budget_ms: int
    spent: list[tuple[str, int]] = field(default_factory=list)

    @classmethod
    def start(cls, budget_ms: int, trace_id: str) -> Deadline:
        return cls(
            at_monotonic=time.monotonic() + budget_ms / 1000,
            trace_id=trace_id,
            budget_ms=budget_ms,
        )

    @property
    def remaining_ms(self) -> int:
        return max(0, int((self.at_monotonic - time.monotonic()) * 1000))

    @property
    def elapsed_ms(self) -> int:
        return max(0, self.budget_ms - self.remaining_ms)

    def slice_ms(self, want_ms: int | float, reserve_ms: int) -> int:
        """Срез от остатка. Урезает запрошенное и НИКОГДА его не расширяет."""
        want = _UNBOUNDED if want_ms == math.inf else int(want_ms)
        return max(0, min(want, self.remaining_ms - reserve_ms))

    def afford(self, floor_ms: int, reserve_ms: int) -> bool:
        """Влезет ли стадия целиком. Начинать то, что не влезет, — трата слота впустую."""
        return self.slice_ms(math.inf, reserve_ms) >= floor_ms

    def record(self, name: str, ms: int) -> None:
        self.spent.append((name, ms))

    def ledger(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, ms in self.spent:
            out[name] = out.get(name, 0) + ms
        return out


@asynccontextmanager
async def stage(
    dl: Deadline, name: str, *, cap_ms: int | float, reserve_ms: int
) -> AsyncIterator[int]:
    """Единственный способ получить таймаут I/O.

    Отдаёт наружу выделенные миллисекунды: стадия может знать, сколько ей
    досталось, но не может выбрать больше.
    """
    ms = dl.slice_ms(cap_ms, reserve_ms)
    if ms <= 0:
        raise DeadlineExceeded(name, ledger=dl.spent)
    started = time.monotonic()
    try:
        async with asyncio.timeout(ms / 1000):
            yield ms
    except TimeoutError as exc:
        raise DeadlineExceeded(name, ledger=dl.spent) from exc
    finally:
        # В finally намеренно: стадия, съевшая бюджет и упавшая, обязана
        # остаться в леджере, иначе постмортем теряет её время.
        dl.record(name, int((time.monotonic() - started) * 1000))


def current() -> Deadline | None:
    return _deadline.get()


def bind(dl: Deadline) -> contextvars.Token:
    return _deadline.set(dl)


def unbind(token: contextvars.Token) -> None:
    _deadline.reset(token)


def assert_off_request_path(what: str) -> None:
    """Запретить операцию внутри запроса.

    Вызывается первой строкой там, где операция принципиально не укладывается
    в бюджет: запуск браузера и любой вызов proxy6 API (жёсткий лимит 3 rps,
    то есть потенциально секунды в очереди).

    Гард закрывает proxy6 полностью — другого входа в API нет. Для браузера он
    неполон, и это надо знать: разработчик, написавший запуск Camoufox прямо в
    обработчике, гарда не задевает. Настоящий запрет там — упаковка: в образе
    ``api`` нет ни ``camoufox``, ни ``playwright``, поэтому браузер не
    импортируется, потому что его там нет.
    """
    if _deadline.get(None) is not None:
        raise OffRequestPathViolation(
            f"{what} is forbidden inside a request-scoped deadline: "
            "browser launch and proxy6 API calls belong to forge only"
        )
