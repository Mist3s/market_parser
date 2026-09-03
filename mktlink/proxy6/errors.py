"""Коды ошибок proxy6 в исключения.

Различение здесь не косметическое: 300 («запрошенное количество превышает
доступное») и 400 («недостаточно средств») требуют разной реакции, и путать
их значит либо покупать в петлю при пустом балансе, либо звать человека
из-за временной нехватки адресов в стране.
"""

from __future__ import annotations


class Proxy6Error(RuntimeError):
    """Любой отказ API proxy6."""

    def __init__(self, error_id: int, key: str, method: str) -> None:
        self.error_id = error_id
        self.key = key
        self.method = method
        super().__init__(f"proxy6 {method}: {error_id} {key}")


class Proxy6AuthError(Proxy6Error):
    """100 — неверный ключ, 105 — доступ с этого IP запрещён.

    Оба означают «дальше идти бессмысленно»: ретраить нельзя, нужен человек.
    """


class Proxy6RateLimited(Proxy6Error):
    """429 — мы превысили 3 rps.

    Не должно случаться: клиент сам себя ограничивает. Срабатывание означает
    второго писателя, то есть нарушение инварианта единственности.
    """


class Proxy6OutOfStock(Proxy6Error):
    """300 — запрошенное количество больше доступного в стране.

    Это НЕ повод для эскалации тарифа сам по себе: сначала стоит проверить,
    не спросили ли мы больше, чем нужно.
    """


class Proxy6InsufficientBalance(Proxy6Error):
    """400 — на счету не хватает.

    Единственная ошибка, которая замораживает и закупку, И продление: частичное
    продление хуже полного отказа, потому что парк истекает неравномерно и
    становится непредсказуемым.
    """


class Proxy6NotFound(Proxy6Error):
    """404 — запрошенного элемента нет.

    На ``delete`` и ``prolong`` это часто нормальный исход повтора: элемент уже
    удалён или уже продлён предыдущей, потерянной попыткой.
    """


class Proxy6BadRequest(Proxy6Error):
    """200-я серия и 110 — мы отправили некорректные параметры. Это наш баг."""


#: Числовые коды из документации. Держим полностью, чтобы неизвестный код
#: отличался от известного-но-необработанного.
ERROR_KEYS: dict[int, str] = {
    30: "unknown",
    100: "auth",
    105: "ip_restricted",
    110: "bad_method",
    200: "bad_count",
    210: "bad_period",
    220: "bad_country",
    230: "bad_ids",
    240: "bad_version",
    250: "bad_descr",
    260: "bad_type",
    270: "bad_port",
    280: "bad_proxy_string",
    300: "out_of_stock",
    400: "insufficient_balance",
    404: "not_found",
    410: "price_error",
    429: "rate_limited",
}

_BY_CODE: dict[int, type[Proxy6Error]] = {
    100: Proxy6AuthError,
    105: Proxy6AuthError,
    110: Proxy6BadRequest,
    200: Proxy6BadRequest,
    210: Proxy6BadRequest,
    220: Proxy6BadRequest,
    230: Proxy6BadRequest,
    240: Proxy6BadRequest,
    250: Proxy6BadRequest,
    260: Proxy6BadRequest,
    270: Proxy6BadRequest,
    280: Proxy6BadRequest,
    300: Proxy6OutOfStock,
    400: Proxy6InsufficientBalance,
    404: Proxy6NotFound,
    429: Proxy6RateLimited,
}


def raise_for(error_id: int, method: str) -> None:
    """Превратить код ответа в исключение нужного класса."""
    key = ERROR_KEYS.get(error_id, "unknown")
    cls = _BY_CODE.get(error_id, Proxy6Error)
    raise cls(error_id, key, method)
