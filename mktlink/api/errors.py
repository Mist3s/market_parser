"""Отбраковка входа: код → HTTP-статус, и один конверт на всё.

Правило, которое стоит назвать прямо: **отказ маркетплейса — никогда не 5xx.**
Если Ozon показал нам капчу, это не «наша серверная ошибка» и не «твоя ссылка
плохая». Клиент, которому в этом случае вернули 422, получает ложь, которую
он не может опровергнуть, и идёт чинить свой корректный URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mktlink.constants import (
    RETRY_AFTER_FETCH_S,
    RETRY_AFTER_MINT_S,
    RETRY_AFTER_SHED_S,
)


@dataclass(frozen=True, slots=True)
class Rejection:
    """Отказ на входе. ``status`` уходит в тело, ``http`` — в статусную строку."""

    status: str
    http: int
    detail: str = ""


#: Таблица отбраковки. Полная: если код не здесь, он не отдаётся клиенту.
TABLE: Final[dict[str, int]] = {
    # 4xx — виноват вход.
    "invalid_url": 422,
    "invalid_budget": 422,
    "host_not_allowed": 422,
    "not_a_product_url": 422,
    "marketplace_disabled": 422,
    "unauthorized": 401,
    "rate_limited": 429,
    # 5xx и 2xx — виноваты мы или маркетплейс, но не вход.
    "capacity_exhausted": 503,
    "deadline_exceeded": 504,
    #: Челлендж на прямом егрессе раскрутки. Не 422: ссылка клиента в порядке,
    #: и говорить ему обратное — врать.
    "unwind_challenged": 504,
    "job_unknown": 404,
}

#: Retry-After по причине. Каждое значение выведено из работы, которую ждут,
#: а не назначено круглым числом.
RETRY_AFTER: Final[dict[str, int]] = {
    # Нужен полный минтинг jar: 9–19 с плюс проверка и живой запрос.
    # Повтор через 3 с получил бы те же 202 ещё несколько раз подряд.
    "no_warm_jar": RETRY_AFTER_MINT_S,
    "breaker_open": RETRY_AFTER_MINT_S,
    "minting": RETRY_AFTER_MINT_S,
    # jar тёплый — нужен только слот или второй прокси.
    "deadline_exhausted": RETRY_AFTER_FETCH_S,
    "spacing_wait_exceeds_budget": RETRY_AFTER_FETCH_S,
    "needs_confirmation": RETRY_AFTER_FETCH_S,
    "all_proxies_busy": RETRY_AFTER_FETCH_S,
    # Сброс нагрузки: ждём ёмкости, а не конкретной работы.
    "capacity": RETRY_AFTER_SHED_S,
}


def http_for(status: str) -> int:
    return TABLE.get(status, 500)


def retry_after_for(reason: str | None) -> int:
    if reason is None:
        return RETRY_AFTER_FETCH_S
    return RETRY_AFTER.get(reason, RETRY_AFTER_FETCH_S)
