"""Класс II — всё, что выводится из ручки ``RESPONSE_BUDGET_MS``.

Модуль чистый: ни I/O, ни времени, ни глобального состояния. Это позволяет
проверять тождество бюджета обычным тестом, а не симуляцией.

Центральное утверждение, которое держит потолок ответа:

    сумма всех cap'ов ступеней + сумма гардов + резидуал == NET_MS

``NET_MS`` — единственное сетевое окно запроса. Из него платятся ожидание
спейсинга, все ступени лестницы и все межступенчатые гарды. Ни одна стадия
не может расширить окно: планировщик раздаёт, а :func:`Deadline.slice_ms`
дополнительно урезает по факту оставшегося времени.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mktlink.constants import (
    FIXED_PRE_MS,
    HOP_MS,
    MIN_INTERVAL_MS,
    PARSE_MS,
    SAFETY_MS,
    SER_MS,
    TAIL_MS,
)

RungKind = Literal["replay", "mint", "render", "enrich"]


@dataclass(frozen=True, slots=True)
class Rung:
    """Ступень лестницы извлечения.

    ``floor_ms`` — ниже этого ступень не запускается вовсе: начинать то, что
    заведомо не успеет, значит потратить прокси-слот впустую. ``ceil_ms`` —
    выше этого ступени не дают, даже когда бюджет позволяет.
    """

    name: str
    kind: RungKind
    floor_ms: int
    ceil_ms: int

    def __post_init__(self) -> None:
        if not 0 < self.floor_ms <= self.ceil_ms:
            raise ValueError(f"{self.name}: floor {self.floor_ms} > ceil {self.ceil_ms}")


@dataclass(frozen=True, slots=True)
class Caps:
    """Производные потолки для одной комбинации (бюджет, маркетплейс, хопы)."""

    budget_ms: int
    marketplace: str
    hops: int
    edge_read_ms: int
    app_ms: int
    io_ms: int
    stage_reserve_ms: int
    net_ms: int


@dataclass(frozen=True, slots=True)
class Plan:
    """Результат планирования: что кому досталось внутри ``net_ms``."""

    rungs: tuple[Rung, ...]
    caps: tuple[int, ...]
    guards: tuple[int, ...]
    prewait_ms: int
    net_ms: int

    def total(self) -> int:
        return sum(self.caps) + sum(self.guards) + self.prewait_ms


#: Лестницы извлечения. Порядок значим: оркестратор идёт сверху вниз.
#:
#: ``enrich`` — всегда последняя и первая, кого выбрасывают при нехватке
#: бюджета: её отсутствие даёт ``legal_name: null``, а не догадку.
#: ``mint`` в лестнице присутствует, но с ``kind='mint'``, и планировщик
#: пути запроса его отфильтровывает: минтинг никогда не идёт по горячему пути.
LADDER: dict[str, tuple[Rung, ...]] = {
    "ozon": (
        Rung("ozon.composer_replay", "replay", 700, 1500),
        Rung("ozon.pdp_html_replay", "replay", 800, 1800),
        Rung("ozon.seller_widget", "enrich", 700, 1500),
        Rung("ozon.mint_with_payload", "mint", 7230, 11420),
    ),
    "wb": (
        Rung("wb.card_detail", "replay", 500, 1200),
        Rung("wb.pdp_html", "replay", 800, 1800),
        Rung("wb.seller_page", "enrich", 600, 1400),
        Rung("wb.mint_with_payload", "mint", 6030, 9920),
    ),
    "ym": (
        Rung("ym.pdp_html", "replay", 1100, 2600),
        Rung("ym.offers_page", "replay", 800, 2400),
        Rung("ym.pdp_html_retry", "replay", 1100, 2600),
        Rung("ym.shop_page", "enrich", 600, 1800),
        Rung("ym.lightjar", "mint", 400, 1500),
    ),
}

#: Опциональная ступень: тёплый Camoufox в forge по RPC-операции ``render``.
#: В дефолтной лестнице её НЕТ — браузера на пути запроса не бывает. Включается
#: флагом заказчика и стоит +5 с к p95.
RENDER_RUNG: dict[str, Rung] = {
    "ym": Rung("ym.render", "render", 1500, 5000),
}

#: Ступени, за которые платит путь запроса.
_REQUEST_KINDS: frozenset[str] = frozenset({"replay", "enrich"})


def stage_reserve_ms(mp: str) -> int:
    """Хвост, который обязан остаться после последней сетевой стадии."""
    return PARSE_MS[mp] + SER_MS + TAIL_MS + SAFETY_MS


def derive(budget_ms: int, mp: str, hops: int) -> Caps:
    """Вывести все производные потолки. Единственный источник этих чисел."""
    if mp not in PARSE_MS:
        raise KeyError(f"unknown marketplace: {mp!r}")
    if hops < 0:
        raise ValueError(f"hops must be >= 0, got {hops}")
    app_ms = budget_ms - 400
    reserve = stage_reserve_ms(mp)
    return Caps(
        budget_ms=budget_ms,
        marketplace=mp,
        hops=hops,
        edge_read_ms=budget_ms - 100,
        app_ms=app_ms,
        io_ms=app_ms - TAIL_MS - SAFETY_MS,
        stage_reserve_ms=reserve,
        net_ms=app_ms - FIXED_PRE_MS - hops * HOP_MS - reserve,
    )


def request_ladder(mp: str, *, with_render: bool = False) -> tuple[Rung, ...]:
    """Ступени, допустимые на пути запроса: без минтинга, и по умолчанию без рендера."""
    rungs = [r for r in LADDER[mp] if r.kind in _REQUEST_KINDS]
    if with_render and mp in RENDER_RUNG:
        # Рендер встаёт перед enrich-ступенью: он даёт данные, она их обогащает.
        tail = [r for r in rungs if r.kind == "enrich"]
        head = [r for r in rungs if r.kind != "enrich"]
        rungs = [*head, RENDER_RUNG[mp], *tail]
    return tuple(rungs)


def plan(budget_ms: int, mp: str, hops: int, *, with_render: bool = False) -> Plan:
    """Расписать ``net_ms`` между ступенями, гардами и ожиданием спейсинга.

    Правило в трёх строках (§3.2):

    1. Ступень 1 получает ``min(ceil, net)`` — она обязана сработать, и если
       весь ``net`` меньше её потолка, он весь достаётся ей. Если этого мало
       для её ``floor``, лестницы нет вовсе: это и есть ``BUDGET_FLOOR_MS``.
    2. Следующие ступени допускаются по своему ``floor``, пока остаётся
       резерв не меньше ``MIN_INTERVAL`` на ожидание спейсинга.
    3. Остаток выше ``MIN_INTERVAL`` раздаётся допущенным ступеням по порядку
       до их ``ceil``; что не разошлось — резидуал ``prewait_ms``.

    Отсюда наблюдение, которое стоит держать в голове при чтении леджера:
    ``P(202 из-за спейсинга) > 0`` тогда и только тогда, когда резидуал меньше
    ``MIN_INTERVAL``, то есть когда лестница упёрлась в свои потолки, а не в
    бюджет.
    """
    net = derive(budget_ms, mp, hops).net_ms
    ladder = request_ladder(mp, with_render=with_render)
    if not ladder:
        raise ValueError(f"no request-path ladder for {mp!r}")
    guard = MIN_INTERVAL_MS[mp]

    first_cap = min(ladder[0].ceil_ms, net)
    if first_cap < ladder[0].floor_ms:
        raise BudgetTooSmall(budget_ms, mp, hops, need=ladder[0].floor_ms, have=net)

    admitted: list[Rung] = [ladder[0]]
    caps: list[int] = [first_cap]
    for cand in ladder[1:]:
        # Гардов ровно n−1, поэтому допуск ещё одной ступени добавляет один гард.
        committed = sum(caps) + guard * len(admitted) + cand.floor_ms
        if net - committed < guard:
            break
        admitted.append(cand)
        caps.append(cand.floor_ms)

    guards = [guard] * (len(admitted) - 1)
    spare = net - sum(caps) - sum(guards) - guard
    for i, rung in enumerate(admitted):
        if spare <= 0:
            break
        room = rung.ceil_ms - caps[i]
        give = min(room, spare)
        caps[i] += give
        spare -= give

    prewait = net - sum(caps) - sum(guards)
    return Plan(
        rungs=tuple(admitted),
        caps=tuple(caps),
        guards=tuple(guards),
        prewait_ms=prewait,
        net_ms=net,
    )


class BudgetTooSmall(ValueError):
    """Бюджета не хватает даже на первую ступень — лестницы не существует."""

    def __init__(self, budget_ms: int, mp: str, hops: int, *, need: int, have: int) -> None:
        self.budget_ms = budget_ms
        self.marketplace = mp
        self.hops = hops
        self.need = need
        self.have = have
        super().__init__(
            f"budget {budget_ms} ms leaves {have} ms of network window for {mp} "
            f"after {hops} hop(s); first rung needs {need} ms"
        )
