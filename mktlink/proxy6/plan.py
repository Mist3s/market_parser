"""Планировщик жизненного цикла прокси. ЧИСТАЯ функция.

Здесь нет ни сети, ни времени, ни базы: на вход — снимок инвентаря, здоровья
и трат, на выход — список команд. Всё, что тратит деньги, проходит через эту
функцию, и потому проверяется обычным тестом, а не наблюдением за счётом.

Модель ленивая, как и просил заказчик: пула нет. На старте переиспользуем то,
что уже куплено; если нечего — покупаем РОВНО ОДИН адрес. Растём только
заменой, никогда не провизионированием впрок.

Три вещи, которые здесь важнее остальных:

**Метка «никогда не продлевать» пишется ДО вывода из обслуживания.** Не после
и не одновременно. Падение в любой момент после первого признака проблемы
оставляет прокси, который не будет продлён: дефолтный режим отказа — «потерять
адрес», никогда не «продлевать плохой вечно».

**Плохой на одном маркетплейсе — не повод списывать адрес.** У Ozon с
датацентрового IP ожидается высокая доля молчаливых отказов; списание по нему
сожгло бы каждый купленный адрес, включая те, что Я.Маркет обслуживает
прекрасно. Списание требует провала на большинстве маркетплейсов.

**Эскалация тарифа чисто качественная и не для всех.** Количественный триггер
(«shared кончились») при пуле из одного адреса мёртв. А Я.Маркету выделенный
IP не помогает вовсе: SmartCaptcha — это челлендж, а не блок по репутации
адреса, и выделенность решения выдать челлендж не меняет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from mktlink.proxy6.descr import Descr, DescrError, is_nonce, is_ours, unpack

#: Сколько нужно провалов подряд, чтобы счесть адрес плохим на маркетплейсе.
#: При 60 запросах в час на маркетплейс окно в 20 попыток набирается за 20
#: минут — это и есть скорость реакции, и её надо знать, а не надеяться.
BAD_WINDOW = 20
BAD_RATE = 0.35

#: Сколько маркетплейсов должны признать адрес плохим, чтобы списать его.
QUORUM_TO_RETIRE = 2

#: Не берём в работу адрес, которому осталось меньше этого: он истечёт
#: посреди запроса, и отказ будет выглядеть как вина прокси.
MIN_REMAINING_S = 900

#: За сколько до конца срока продлевать.
RENEW_HORIZON_S = 3 * 86400

#: Маркетплейсы, которым выделенный IPv4 не помогает в принципе.
NO_ESCALATION: frozenset[str] = frozenset({"ym"})


class Op(StrEnum):
    BUY = "buy"
    PROLONG = "prolong"
    SETDESCR = "setdescr"
    DELETE = "delete"
    MARK_NEVER_RENEW = "mark_never_renew"
    ADOPT_PENDING = "adopt_pending"
    NOTE_FOREIGN = "note_foreign"


@dataclass(frozen=True, slots=True)
class Command:
    op: Op
    p6_id: int | None = None
    version: int | None = None
    period_days: int | None = None
    descr: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Health:
    """Здоровье пары (прокси, маркетплейс)."""

    ok_n: int = 0
    bad_n: int = 0
    captcha_n: int = 0

    @property
    def total(self) -> int:
        return self.ok_n + self.bad_n

    @property
    def decided(self) -> bool:
        """Хватает ли выборки, чтобы вообще судить."""
        return self.total >= BAD_WINDOW

    @property
    def bad(self) -> bool:
        return self.decided and (self.bad_n / self.total) > (1 - BAD_RATE)


@dataclass(frozen=True, slots=True)
class Inventory:
    """Снимок одного адреса у proxy6 плюс наше знание о нём."""

    p6_id: int
    descr_raw: str
    version: int
    unixtime_end: int
    active: bool
    never_renew_local: bool = False
    health: dict[str, Health] = field(default_factory=dict)

    @property
    def ours(self) -> bool:
        return is_ours(self.descr_raw)

    @property
    def nonce(self) -> bool:
        return is_nonce(self.descr_raw)

    def parsed(self) -> Descr | None:
        try:
            return unpack(self.descr_raw)
        except DescrError:
            return None


@dataclass(frozen=True, slots=True)
class Situation:
    """Всё, что планировщик знает о мире."""

    now: int
    inventory: list[Inventory]
    spent_kop_30d: int
    cap_kop_30d: int = 60_000
    period_days: int = 7
    #: Оценка цены одной покупки. Берётся из getprice ДО решения.
    quote_kop: int = 870
    #: Маркетплейсы, для которых включена эскалация тарифа.
    escalate: frozenset[str] = frozenset()


def usable(inv: Inventory, now: int) -> bool:
    """Годен ли адрес к работе прямо сейчас."""
    if not inv.ours or inv.nonce or not inv.active:
        return False
    d = inv.parsed()
    if d is None or d.retired:
        return False
    return inv.unixtime_end - now >= MIN_REMAINING_S


def should_retire(inv: Inventory) -> bool:
    """Списывать ли адрес.

    Требуется кворум: плохой на ОДНОМ маркетплейсе — это, скорее всего,
    свойство маркетплейса, а не адреса.
    """
    bad = [mp for mp, h in inv.health.items() if h.bad]
    return len(bad) >= QUORUM_TO_RETIRE


def plan_bootstrap(s: Situation) -> list[Command]:
    """Что сделать на старте.

    Порядок команд значим: сперва разметка того, что уже есть, и только
    потом — решение о покупке, потому что покупать надо лишь тогда, когда
    после разметки не осталось ни одного годного адреса.
    """
    cmds: list[Command] = []

    for inv in s.inventory:
        if not inv.ours:
            # Чужой адрес: считаем, докладываем, НИКОГДА не трогаем.
            # Переименование чужого в наш namespace было бы захватом
            # оплаченной кем-то собственности.
            cmds.append(Command(Op.NOTE_FOREIGN, inv.p6_id, descr=inv.descr_raw))
            continue

        if inv.nonce:
            # Осиротевшая закупка: ответ потерялся, а адрес пришёл.
            cmds.append(
                Command(
                    Op.SETDESCR,
                    inv.p6_id,
                    descr="adopt-orphan",
                    reason="buy response was lost; the proxy landed anyway",
                )
            )
            continue

        d = inv.parsed()
        if d is None:
            # Битая грамматика трактуется как списанный и непродлеваемый:
            # безопасная сторона ошибки — потерять адрес, а не продлить
            # неизвестно что.
            cmds.append(
                Command(Op.MARK_NEVER_RENEW, inv.p6_id, reason="malformed descr")
            )
            continue

        if d.renew == "R" and not inv.never_renew_local:
            # ИНВЕРСИЯ ДЕФОЛТА. Адрес, найденный продлеваемым, но без нашей
            # локальной записи, — это либо полная потеря стора, либо чужая
            # покупка в нашем namespace. Благословлять его нельзя: помечаем
            # непродлеваемым и требуем явного усыновления человеком.
            cmds.append(
                Command(
                    Op.ADOPT_PENDING,
                    inv.p6_id,
                    reason="renewable on proxy6 but unknown locally",
                )
            )

    if any(usable(i, s.now) for i in s.inventory):
        return cmds

    # Годных нет — покупаем РОВНО ОДИН.
    cmds.extend(plan_buy(s, marketplace=None, reason="no usable proxy at bootstrap"))
    return cmds


def plan_buy(s: Situation, *, marketplace: str | None, reason: str) -> list[Command]:
    """Купить один адрес, если это разрешено деньгами."""
    if s.spent_kop_30d + s.quote_kop > s.cap_kop_30d:
        return []
    version = 4 if (marketplace and marketplace in s.escalate) else 3
    return [
        Command(
            Op.BUY,
            version=version,
            period_days=s.period_days,
            reason=reason,
        )
    ]


def plan_replacement(s: Situation, inv: Inventory) -> list[Command]:
    """Заменить плохой адрес.

    Порядок здесь — не стиль, а гарантия. Метка ставится ПЕРВОЙ: если всё
    упадёт сразу после неё, мы потеряем адрес, но не продлим плохой.
    Старый продолжает обслуживать, пока новый не куплен и не проверен —
    иначе замена означала бы окно полного отсутствия сервиса.
    """
    cmds: list[Command] = [
        Command(Op.MARK_NEVER_RENEW, inv.p6_id, reason="bad on a quorum of marketplaces"),
    ]
    d = inv.parsed()
    if d is not None:
        cmds.append(
            Command(
                Op.SETDESCR,
                inv.p6_id,
                descr="retire",
                reason="role -> r, renew -> X",
            )
        )
    cmds.extend(plan_buy(s, marketplace=None, reason=f"replacing {inv.p6_id}"))
    return cmds


def plan_renewals(s: Situation) -> list[Command]:
    """Кого продлевать. Двусторонний veto: нужны ОБА разрешения.

    Ни локальная запись, ни живой ``descr`` в одиночку продление не разрешают.
    Так восстановление стора из старого бэкапа не может воскресить сожжённый
    адрес: живой ``descr`` всё ещё говорит ``X``.
    """
    out: list[Command] = []
    for inv in s.inventory:
        if not inv.ours or inv.nonce:
            continue
        d = inv.parsed()
        if d is None:
            continue
        if inv.never_renew_local or d.renew == "X":
            continue
        if d.retired:
            continue
        if inv.unixtime_end - s.now > RENEW_HORIZON_S:
            continue
        if s.spent_kop_30d + s.quote_kop > s.cap_kop_30d:
            continue
        out.append(
            Command(
                Op.PROLONG,
                inv.p6_id,
                period_days=s.period_days,
                reason="expiring within the horizon",
            )
        )
    return out


def plan_tier(
    marketplace: str, health_v3: Health, health_v4: Health | None
) -> Literal["escalate", "deescalate", "hold"]:
    """Решение по тарифу для маркетплейса.

    Количественный триггер («shared кончились») при пуле из одного адреса
    мёртв, поэтому решение чисто качественное. И оно не для всех: у
    Я.Маркета антибот — челлендж, а не блок по репутации адреса, так что
    выделенный IPv4 не меняет ничего, кроме счёта.
    """
    if marketplace in NO_ESCALATION:
        return "hold"
    if not health_v3.decided:
        return "hold"
    if not health_v3.bad:
        return "deescalate" if health_v4 is not None else "hold"
    if health_v4 is None:
        return "escalate"
    if not health_v4.decided:
        return "hold"
    if health_v4.bad:
        # Плечо не окупилось: платим вчетверо, а результат тот же. Значит
        # мешает не сосед по shared-адресу, а сам датацентр, и правильный
        # ответ — перестать платить за выделенность, а не платить ещё больше.
        return "deescalate"
    # Эскалация сработала: остаёмся на v4.
    return "hold"
