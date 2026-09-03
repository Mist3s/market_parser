"""Клиент proxy6: ключ в path, потолок 3 rps, auto_prolong никогда."""

from __future__ import annotations

import asyncio
import time

import pytest

from mktlink.proxy6.client import MIN_GAP_S, Proxy6Client, ProxyRow, redact
from mktlink.proxy6.errors import (
    Proxy6AuthError,
    Proxy6InsufficientBalance,
    Proxy6NotFound,
    Proxy6OutOfStock,
    Proxy6RateLimited,
)
from mktlink.timing.deadline import Deadline, OffRequestPathViolation, bind, unbind

KEY = "secret-key-0123"


class FakeTransport:
    """Пишет вызовы и отдаёт заготовленные ответы."""

    def __init__(self, *responses: dict) -> None:
        self.urls: list[str] = []
        self.responses = list(responses) or [{"status": "yes"}]

    async def __call__(self, url: str) -> dict:
        self.urls.append(url)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def client(*responses: dict) -> tuple[Proxy6Client, FakeTransport]:
    t = FakeTransport(*responses)
    return Proxy6Client(KEY, transport=t), t


async def test_api_key_goes_into_the_path_not_the_query() -> None:
    c, t = client({"status": "yes", "count": 5})
    await c.getcount(version=3)
    assert t.urls[0].startswith(f"https://px6.link/api/{KEY}/getcount/")
    assert f"key={KEY}" not in t.urls[0]


async def test_redact_removes_the_key_from_anything_we_log() -> None:
    url = f"https://px6.link/api/{KEY}/getproxy/?state=all"
    assert KEY not in redact(url, KEY)
    assert "***" in redact(url, KEY)


async def test_buy_never_sends_auto_prolong() -> None:
    """API не умеет его выключить после покупки, поэтому мы его не включаем."""
    c, t = client({"status": "yes", "count": 1, "list": []})
    await c.buy(count=1, period=7, version=3, descr="mp1.ord.260902.7Q3XK9")
    assert "auto_prolong" not in t.urls[0]
    assert "version=3" in t.urls[0]
    assert "period=7" in t.urls[0]
    assert "country=ru" in t.urls[0]


async def test_rate_limiter_keeps_us_under_three_rps() -> None:
    c, _ = client({"status": "yes", "count": 1})
    started = time.monotonic()
    for _ in range(3):
        await c.getcount(version=3)
    elapsed = time.monotonic() - started
    # Три вызова — минимум два зазора между ними.
    assert elapsed >= 2 * MIN_GAP_S * 0.9, elapsed
    assert 1 / MIN_GAP_S <= 3.0, "заявленный потолок не должен превышать 3 rps"


async def test_concurrent_calls_are_serialised_not_parallel() -> None:
    """Процесс-локальный ограничитель корректен только при одной операции в полёте."""
    c, t = client({"status": "yes", "count": 1})
    started = time.monotonic()
    await asyncio.gather(*(c.getcount(version=3) for _ in range(3)))
    elapsed = time.monotonic() - started
    assert elapsed >= 2 * MIN_GAP_S * 0.9, elapsed
    assert len(t.urls) == 3


async def test_calling_proxy6_inside_a_request_is_refused() -> None:
    c, _ = client({"status": "yes"})
    dl = Deadline.start(5000, "t")
    token = bind(dl)
    try:
        with pytest.raises(OffRequestPathViolation):
            await c.getcount(version=3)
    finally:
        unbind(token)


@pytest.mark.parametrize(
    ("code", "exc"),
    [
        (100, Proxy6AuthError),
        (105, Proxy6AuthError),
        (300, Proxy6OutOfStock),
        (400, Proxy6InsufficientBalance),
        (404, Proxy6NotFound),
        (429, Proxy6RateLimited),
    ],
)
async def test_error_codes_become_distinct_exceptions(code: int, exc: type) -> None:
    """300 и 400 требуют разной реакции: путать их значит либо покупать в
    петлю при пустом балансе, либо звать человека из-за нехватки адресов."""
    c, _ = client({"status": "no", "error_id": code, "error": "x"})
    with pytest.raises(exc):
        await c.getcount(version=3)


async def test_getproxy_accepts_both_dict_and_list_shapes() -> None:
    row = {
        "id": "11",
        "version": "3",
        "ip": "1.2.3.4",
        "host": "1.2.3.4",
        "port": "8000",
        "user": "u",
        "pass": "p",
        "type": "http",
        "country": "ru",
        "unixtime_end": "1800000000",
        "descr": "mp1.mp.s.a.R.ru.g01",
        "active": "1",
    }
    for shape in ({"11": row}, [row]):
        c, _ = client({"status": "yes", "list": shape})
        rows = await c.getproxy()
        assert len(rows) == 1
        assert rows[0].id == 11 and rows[0].version == 3
        assert rows[0].proxy_url() == "http://u:p@1.2.3.4:8000"


async def test_proxy_row_has_no_state_and_no_auto_prolong() -> None:
    """Чего API не отдаёт — того у нас и нет.

    Состояние живёт в нашем сторе; включённость автопродления не показывается
    вовсе и выводится поведенчески, по прыжку unixtime_end без нашего prolong.
    """
    fields = set(ProxyRow.__dataclass_fields__)
    assert "state" not in fields
    assert "auto_prolong" not in fields


async def test_setdescr_requires_exactly_one_selector() -> None:
    c, _ = client({"status": "yes", "count": 1})
    with pytest.raises(ValueError):
        await c.setdescr(new="mp1.mp.s.a.R.ru.g01")
    with pytest.raises(ValueError):
        await c.setdescr(new="mp1.mp.s.a.R.ru.g01", ids=[1], old="x")
    assert await c.setdescr(new="mp1.mp.s.a.R.ru.g01", ids=[1]) == 1


async def test_setdescr_refuses_an_over_long_tag() -> None:
    c, _ = client({"status": "yes", "count": 1})
    with pytest.raises(ValueError):
        await c.setdescr(new="x" * 51, ids=[1])


async def test_delete_requires_exactly_one_selector() -> None:
    c, _ = client({"status": "yes", "count": 1})
    with pytest.raises(ValueError):
        await c.delete()
    with pytest.raises(ValueError):
        await c.delete(ids=[1], descr="x")


async def test_empty_api_key_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        Proxy6Client("")


async def test_check_answers_proxy6s_view_only() -> None:
    c, _ = client({"status": "yes", "proxy_id": 1, "proxy_status": True})
    assert await c.check(proxy_id=1) is True
    c, _ = client({"status": "yes", "proxy_id": 1, "proxy_status": False})
    assert await c.check(proxy_id=1) is False
