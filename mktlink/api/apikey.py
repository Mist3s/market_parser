"""Защита входа: ключи и учёт в кредитах, а не в запросах.

Почему кредиты, а не «N запросов в минуту». Запросы стоят разного: попадание
в кэш не стоит ничего, холодный запрос стоит слота прокси, а принудительный
сброс кэша стоит слота гарантированно. Лимит, считающий их одинаково, либо
душит дешёвый трафик, либо пропускает дорогой.

Асимметрия отказов намеренная и записана здесь, потому что она неочевидна:

* Лимит **на ключ** — fail-open. Сбой при проверке одного ключа не должен
  ронять обслуживание остальных.
* Глобальный потолок — **fail-closed**. Если мы не можем посчитать общий
  расход, безопасное поведение — отказать: неучтённый расход тратит прокси
  и деньги.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Цена операции в кредитах. Пропорциональна тому, что она реально тратит.
COST: dict[str, int] = {
    "cache_hit": 1,
    "stale": 2,
    "redirect_unwind": 2,
    "cold": 10,
    #: Вчетверо дороже холодного и сознательно карательно: сброс кэша — самый
    #: дешёвый способ устроить нам DoS чужими руками.
    "force_refresh": 40,
    "invalid_url": 5,
    "unsupported_marketplace": 3,
    "not_found_confirmed": 10,
    #: Карточка обычного магазина: один прямой запрос с нашего адреса, без
    #: прокси и без кредитов скрейпинг-API. Дороже кэша, много дешевле
    #: холодной карточки маркетплейса.
    "shop_cold": 3,
    #: Опрос статуса задания бесплатен. Иначе вежливый клиент, опрашивающий
    #: раз в секунду, выбирает свою квоту и получает 429 на следующий
    #: осмысленный запрос.
    "job_poll": 0,
}

#: Дневная квота по умолчанию.
DEFAULT_DAILY_CREDITS = 2000
#: Потолок на минуту, чтобы всплеск не съел день за раз.
DEFAULT_RPM_CREDITS = 200


class Unauthorized(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, scope: str, retry_after_s: int) -> None:
        self.scope = scope
        self.retry_after_s = retry_after_s
        super().__init__(f"rate limited by {scope}")


@dataclass(frozen=True, slots=True)
class KeyInfo:
    key_id: str
    daily_quota: int


@dataclass
class Admission:
    """Учёт расхода. Всё состояние — в SQLite плюс окно в памяти."""

    conn: sqlite3.Connection
    rpm_credits: int = DEFAULT_RPM_CREDITS
    _window: dict[str, list[tuple[float, int]]] = field(default_factory=dict)

    def authenticate(self, presented: str | None) -> KeyInfo:
        """Проверить ключ.

        Сравнение постоянного времени: иначе по времени ответа подбирается
        префикс, и «секрет» перестаёт быть секретом.
        """
        if not presented:
            raise Unauthorized("no api key")
        digest = hashlib.sha256(presented.encode()).hexdigest()
        for row in self.conn.execute(
            "SELECT key_id, hash, daily_quota, disabled FROM api_key"
        ):
            if hmac.compare_digest(row["hash"], digest):
                if row["disabled"]:
                    raise Unauthorized("key disabled")
                return KeyInfo(row["key_id"], int(row["daily_quota"]))
        raise Unauthorized("unknown api key")

    def charge(self, key: KeyInfo, kind: str) -> int:
        """Списать стоимость операции. Бросает :class:`RateLimited`."""
        cost = COST.get(kind, COST["cold"])
        if cost == 0:
            return 0

        now = time.monotonic()
        window = [(t, c) for t, c in self._window.get(key.key_id, []) if now - t < 60]
        if sum(c for _, c in window) + cost > self.rpm_credits:
            raise RateLimited("per-minute", retry_after_s=60)
        window.append((now, cost))
        self._window[key.key_id] = window

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT credits FROM key_usage WHERE key_id = ? AND day = ?", (key.key_id, day)
        ).fetchone()
        used = int(row["credits"]) if row else 0
        if used + cost > key.daily_quota:
            raise RateLimited("daily", retry_after_s=_seconds_to_midnight())

        self.conn.execute(
            "INSERT INTO key_usage (key_id, day, credits) VALUES (?, ?, ?)"
            " ON CONFLICT(key_id, day) DO UPDATE SET credits = credits + excluded.credits",
            (key.key_id, day, cost),
        )
        return cost

    def refund(self, key: KeyInfo, spent: int, actual_kind: str) -> None:
        """Вернуть переплату, когда операция оказалась дешевле ожидаемой.

        Списываем по худшему сценарию ДО работы, потому что иначе абьюзер,
        чьи запросы всегда падают, не платит ничего. Возврат делает честным
        обычного клиента.
        """
        actual = COST.get(actual_kind, spent)
        if actual >= spent:
            return
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        self.conn.execute(
            "UPDATE key_usage SET credits = MAX(0, credits - ?) WHERE key_id = ? AND day = ?",
            (spent - actual, key.key_id, day),
        )

    def usage(self, key_id: str) -> int:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT credits FROM key_usage WHERE key_id = ? AND day = ?", (key_id, day)
        ).fetchone()
        return int(row["credits"]) if row else 0


def register_key(
    conn: sqlite3.Connection, key_id: str, secret: str, *, quota: int | None = None
) -> None:
    conn.execute(
        "INSERT INTO api_key (key_id, hash, daily_quota) VALUES (?, ?, ?)"
        " ON CONFLICT(key_id) DO UPDATE SET hash = excluded.hash",
        (key_id, hashlib.sha256(secret.encode()).hexdigest(), quota or DEFAULT_DAILY_CREDITS),
    )


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
    return max(1, int(tomorrow - now.timestamp()))
