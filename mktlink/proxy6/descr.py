"""Кодек ``descr`` — единственного writable-поля прокси у proxy6.

Почему это вообще важно. API proxy6 не умеет выключать ``auto_prolong`` после
покупки, поэтому мы его **никогда не передаём**: продление становится нашим
действием, и «никогда не продлевать» превращается в «не делать действие».
Метку «не продлевать» надо где-то хранить так, чтобы она пережила потерю
локального состояния, а единственное поле, которое proxy6 разрешает писать, —
это ``descr`` длиной до 50 символов.

Отсюда конструкция: поля фиксированной ширины, ровно 19 символов, поэтому
``getproxy(descr=...)`` работает как запрос к индексу, а не как поиск подстроки.

Veto двусторонний: продлевать можно только если И локальная запись, И живой
``descr`` от proxy6 говорят «можно». Любой источник в одиночку запрещает трату.
Флаг ``X`` пишется ДО вывода прокси из обслуживания, поэтому падение в любой
момент оставляет прокси, который не будет продлён: дефолтный режим отказа —
«потерять прокси», никогда не «продлевать плохой вечно».

Цена позиционного тэга названа прямо: ``RENEW`` сидит в одной строке
фиксированной ширины с ``ROLE``, поэтому любая смена роли перезаписывает и
флаг продления. «Роль сменить, флаг оставить как был» одним ``setdescr`` не
выражается — каждый ``setdescr`` есть read-modify-write от текущего ``descr``
из последнего ``getproxy``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

NAMESPACE: Final[str] = "mp1."
DESCR_LEN: Final[int] = 19
NONCE_LEN: Final[int] = 21
#: proxy6 отвергает descr длиннее этого.
PROXY6_DESCR_MAX: Final[int] = 50

Marketplace = Literal["oz", "wb", "ym", "mp", "xx"]
Tier = Literal["s", "d"]
Role = Literal["a", "w", "r"]
Renew = Literal["R", "X"]

#: ``mp`` — обслуживает все три маркетплейса в скоупе, и это ДЕФОЛТ ленивой
#: модели: один прокси на всё. Партиционирование по маркетплейсам осмысленно
#: только когда пул вырастет, а он растёт лишь заменой.
MK_VALUES: Final[frozenset[str]] = frozenset({"oz", "wb", "ym", "mp", "xx"})

_GRAMMAR: Final[re.Pattern[str]] = re.compile(
    r"^mp1\.(?P<mk>oz|wb|ym|mp|xx)\.(?P<tier>[sd])\.(?P<role>[awr])\.(?P<renew>[RX])"
    r"\.(?P<geo>ru)\.g(?P<gen>\d{2})$"
)

#: Массовое удаление защищено регуляркой в коде, а не аккуратностью оператора.
#: Пропускает только списанный (``r``) И помеченный never-renew (``X``).
DELETABLE: Final[re.Pattern[str]] = re.compile(
    r"^mp1\.(?:oz|wb|ym|mp|xx)\.[sd]\.r\.X\.ru\.g\d{2}$"
)

_NONCE: Final[re.Pattern[str]] = re.compile(r"^mp1\.ord\.(?P<ymd>\d{6})\.(?P<rand>[A-Z2-9]{6})$")

#: Алфавит nonce: 32 символа, из которых исключены 0/1/I/O — человек читает их
#: с логов и из панели proxy6, и путаница здесь стоит денег.
NONCE_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

#: ЕДИНСТВЕННЫЙ литерал с ``.R.`` во всём проекте. CI-ассерт проверяет это
#: grep'ом по пакету: источник «продлеваемости» должен быть один и именованный.
BIRTH_DESCR: Final[str] = "mp1.xx.{tier}.w.R.ru.g{gen:02d}"


class DescrError(ValueError):
    """``descr`` не разбирается нашей грамматикой."""


@dataclass(frozen=True, slots=True)
class Descr:
    """Разобранный классовый тэг."""

    mk: str
    tier: Tier
    role: Role
    renew: Renew
    gen: int

    @property
    def version(self) -> int:
        """Тариф proxy6: 3 = IPv4 Shared, 4 = IPv4."""
        return 3 if self.tier == "s" else 4

    @property
    def never_renew(self) -> bool:
        return self.renew == "X"

    @property
    def retired(self) -> bool:
        return self.role == "r"

    def gen_tag(self) -> str:
        return f"g{self.gen:02d}"


def is_ours(descr: str) -> bool:
    """Наш ли это прокси.

    Namespace guard: всё, что не начинается с ``mp1.``, — FOREIGN. Такие
    прокси считаются и попадают в отчёт бутстрапа, но никогда не продлеваются,
    не удаляются и **никогда не получают** ``setdescr``. Это защита прокси,
    купленных человеком на том же аккаунте: переименование чужого в наш
    namespace было бы захватом чужой оплаченной собственности.
    """
    return descr.startswith(NAMESPACE)


def is_nonce(descr: str) -> bool:
    return _NONCE.match(descr) is not None


def unpack(descr: str) -> Descr:
    """Разобрать классовый тэг. Бросает :class:`DescrError` на чужом и на битом."""
    m = _GRAMMAR.match(descr)
    if m is None:
        raise DescrError(f"not a well-formed mktlink descr: {descr!r}")
    return Descr(
        mk=m["mk"],
        tier=m["tier"],  # type: ignore[arg-type]
        role=m["role"],  # type: ignore[arg-type]
        renew=m["renew"],  # type: ignore[arg-type]
        gen=int(m["gen"]),
    )


def pack_birth(version: int, gen: int) -> str:
    """Тэг, который вешается на покупку в момент приёмки (переход L2).

    Роль ``w`` (waiting) и ``mk = xx``: прокси принят, но ещё не назначен
    маркетплейсу и ещё не валидирован. Это единственное место, где рождается
    ``RENEW='R'``.
    """
    if version not in (3, 4):
        raise ValueError(f"proxy6 version must be 3 (IPv4 Shared) or 4 (IPv4), got {version}")
    if not 0 <= gen <= 99:
        raise ValueError(f"gen must fit two digits, got {gen}")
    out = BIRTH_DESCR.format(tier="s" if version == 3 else "d", gen=gen)
    _check_len(out)
    return out


def pack_write(cur: Descr, *, role: Role, mk: str | None = None, local_veto: bool = False) -> str:
    """Собрать новый тэг из текущего — read-modify-write.

    ``local_veto`` — вторая половина двустороннего veto: если локальная запись
    говорит «не продлевать», флаг ``X`` пишется независимо от того, что сейчас
    в живом ``descr``. Обратного хода нет: ``X`` побеждает всегда, потому что
    восстановление локального стора из старого бэкапа не должно воскрешать
    сожжённый IP.
    """
    if role not in ("a", "w", "r"):
        raise ValueError(f"role must be one of a|w|r, got {role!r}")
    target_mk = mk if mk is not None else cur.mk
    if target_mk not in MK_VALUES:
        raise ValueError(f"mk must be one of {sorted(MK_VALUES)}, got {target_mk!r}")
    renew: Renew = "X" if (cur.renew == "X" or local_veto) else "R"
    out = f"mp1.{target_mk}.{cur.tier}.{role}.{renew}.ru.{cur.gen_tag()}"
    _check_len(out)
    return out


def pack_nonce(when: date, rand: str) -> str:
    """Транзиентный тэг закупки — идемпотентность при потерянном ответе.

    Закупка помечается уникальным nonce ДО вызова ``buy``. Если ответ потерян,
    ``getproxy(descr=<nonce>)`` показывает, состоялась ли она. Обратное неверно
    и это важно: пустой ответ НЕ доказывает, что закупки не было — она может
    быть ещё в полёте на стороне proxy6, поэтому nonce нельзя переиспользовать
    сразу, а только после выдержки.
    """
    if len(rand) != 6 or any(c not in NONCE_ALPHABET for c in rand):
        raise ValueError(f"nonce rand must be 6 chars from {NONCE_ALPHABET!r}, got {rand!r}")
    out = f"mp1.ord.{when:%y%m%d}.{rand}"
    if len(out) != NONCE_LEN:
        raise AssertionError(f"nonce must be {NONCE_LEN} chars, got {len(out)}: {out!r}")
    return out


def is_deletable(descr: str) -> bool:
    """Пропускает только списанный и помеченный never-renew.

    Удаление неистёкшего прокси ничего не покупает: возвратов у proxy6 нет,
    оплаченные дни просто сгорают. Поэтому проверка срока — отдельное условие
    на стороне вызывающего, а эта регулярка отвечает только за состояние.
    """
    return DELETABLE.match(descr) is not None


def _check_len(descr: str) -> None:
    if len(descr) != DESCR_LEN:
        raise AssertionError(
            f"descr must be exactly {DESCR_LEN} chars, got {len(descr)}: {descr!r}"
        )
    if len(descr) > PROXY6_DESCR_MAX:  # pragma: no cover - следует из предыдущего
        raise AssertionError(f"descr exceeds proxy6 limit of {PROXY6_DESCR_MAX}: {descr!r}")
