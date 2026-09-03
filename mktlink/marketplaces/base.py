"""Контракт экстрактора и проход по лестнице.

Оркестратор лестницы живёт здесь, потому что он одинаков для всех трёх
маркетплейсов: различаются только ступени и предикаты классификации.

Два свойства прохода, каждое обязательно:

* **Ступень не начинается, если её пол не влезает в остаток.** Начинать то,
  что заведомо не успеет, значит потратить слот прокси и ничего не получить.
* **Ступени сливаются, а не заменяют друг друга.** Вторая может дать имя,
  когда первая дала только продавца. Начинать заново при половинном успехе —
  выбрасывать уже оплаченный результат.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from mktlink.budget import Plan, Rung, plan, stage_reserve_ms
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.timing.deadline import Deadline, DeadlineExceeded, stage


@dataclass(frozen=True, slots=True)
class RungResult:
    """Что вернула одна ступень."""

    verdict: Verdict
    name: str | None = None
    seller_name: str | None = None
    seller_id: str | None = None
    seller_status: SellerStatus = SellerStatus.UNKNOWN_LAYOUT
    seller_source: str | None = None
    legal_name: str | None = None
    raw: Any = None

    @property
    def empty(self) -> bool:
        """Ступень не нашла НИЧЕГО.

        Такая ступень не имеет права менять диагноз: если первая сказала
        SILENT_EMPTY или SCHEMA_DRIFT, а вторая просто ничего не извлекла,
        итогом обязан остаться диагноз первой. Иначе блок и дрейф
        превращаются в «частичный успех», и адрес штрафуется не за то.
        """
        return not (self.name or self.seller_name or self.legal_name)

    @property
    def complete(self) -> bool:
        return bool(self.name) and self.seller_status in (
            SellerStatus.RESOLVED,
            SellerStatus.FIRST_PARTY,
            SellerStatus.NO_OFFERS,
        )


class RungFn(Protocol):
    async def __call__(
        self, dl: Deadline, ctx: Context, cap_ms: int, prev: RungResult | None
    ) -> RungResult: ...


@dataclass(slots=True)
class Context:
    """Всё, что нужно ступени, кроме бюджета."""

    marketplace: str
    canonical_url: str
    ids: dict[str, str]
    offer: tuple[tuple[str, str], ...] = ()
    #: Идентификаторы, к которым обязан быть привязан продавец.
    anchor_ids: frozenset[str] = frozenset()


class Extractor(Protocol):
    """Один маркетплейс."""

    marketplace: str

    def rung_fn(self, rung: Rung) -> RungFn: ...


def merge(a: RungResult | None, b: RungResult) -> RungResult:
    """Слить результаты ступеней, не теряя уже добытого и не стирая диагноз."""
    if a is None:
        return b
    if b.empty and not a.empty:
        # Пустая ступень ничего не добавила — и вердикт менять не вправе.
        return a
    if b.empty and a.empty:
        # Обе пусты: сохраняем ПЕРВЫЙ диагноз. Он ближе к причине.
        return a
    return RungResult(
        verdict=b.verdict if b.verdict is not Verdict.OK else (a.verdict if a.name else b.verdict),
        name=a.name or b.name,
        seller_name=a.seller_name or b.seller_name,
        seller_id=a.seller_id or b.seller_id,
        seller_status=(
            a.seller_status
            if a.seller_status in (SellerStatus.RESOLVED, SellerStatus.FIRST_PARTY)
            else b.seller_status
        ),
        seller_source=a.seller_source or b.seller_source,
        legal_name=a.legal_name or b.legal_name,
        raw=a.raw if a.raw is not None else b.raw,
    )


async def run_ladder(
    dl: Deadline,
    extractor: Extractor,
    ctx: Context,
    budget_ms: int,
    hops: int,
) -> tuple[RungResult, str | None]:
    """Пройти лестницу. Возвращает слитый результат и имя последней ступени."""
    p: Plan = plan(budget_ms, ctx.marketplace, hops)
    reserve = stage_reserve_ms(ctx.marketplace)
    best: RungResult | None = None
    last: str | None = None

    for i, (rung, cap) in enumerate(zip(p.rungs, p.caps, strict=True)):
        # Резерв под всё, что идёт ПОСЛЕ этой ступени: хвост плюс полы
        # оставшихся ступеней с их гардами. Отсюда автоматическая деградация:
        # запрос, потерявший время раньше, просто получает меньше ступеней.
        tail = sum(r.floor_ms for r in p.rungs[i + 1 :]) + sum(p.guards[i:])
        if not dl.afford(rung.floor_ms, reserve + tail):
            break

        try:
            async with stage(dl, rung.name, cap_ms=cap, reserve_ms=reserve + tail):
                res = await extractor.rung_fn(rung)(dl, ctx, cap, best)
        except DeadlineExceeded:
            # Наш бюджет, а не вина прокси: вердикт это фиксирует.
            best = merge(best, RungResult(verdict=Verdict.BUDGET_EXHAUSTED))
            break

        last = rung.name
        best = merge(best, res)

        if best.complete:
            return replace(best, verdict=Verdict.OK), last

        # Блок и капча — не повод идти по лестнице дальше тем же путём:
        # следующая ступень получит то же самое и потратит ещё один слот.
        if res.verdict in (Verdict.CAPTCHA, Verdict.HTTP_429):
            break

    if best is None:
        return RungResult(verdict=Verdict.BUDGET_EXHAUSTED), last
    if best.name:
        return replace(best, verdict=Verdict.PARTIAL), last
    # Ничего не найдено: отдаём накопленный диагноз, а не «частичный успех».
    return best, last
