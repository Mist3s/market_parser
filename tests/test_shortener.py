"""Сокращалка: формы ответов и отказов, все замеренные.

Сети тесты не касаются — обе реализации принимают ``sender``.
"""

from __future__ import annotations

import pytest

from mktlink.egress.shortener import PROVIDERS, ShortenFailed, shorten


def _sender(status: int, body: str, seen: list | None = None):
    async def send(url, *, timeout_ms, **kw):
        if seen is not None:
            seen.append({"url": url, "timeout_ms": timeout_ms, "kw": kw})
        return status, body

    return send


@pytest.mark.asyncio
async def test_clck_returns_the_short_link() -> None:
    seen: list = []
    got = await shorten(
        "https://www.ozon.ru/product/x/?a=b",
        provider="clck",
        sender=_sender(200, "https://clck.ru/3VcG4Y\n", seen),
    )
    assert got == "https://clck.ru/3VcG4Y"
    # Целевой URL закодирован целиком: иначе его query оборвётся об наш.
    assert "https%3A%2F%2Fwww.ozon.ru%2Fproduct%2Fx%2F%3Fa%3Db" in seen[0]["url"]


@pytest.mark.asyncio
async def test_error_with_status_200_is_still_an_error() -> None:
    """ЗАМЕР: is.gd отдаёт ``Error, database insert failed`` со статусом 200.

    Без проверки формы эта строка ушла бы в скрейпинг-API как «адрес», и
    отказ пришёл бы позже, в другом месте и с другой причиной.
    """
    sender = _sender(200, "Error, database insert failed")
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="clck", sender=sender)


@pytest.mark.asyncio
async def test_html_page_is_not_a_short_link() -> None:
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="clck", sender=_sender(200, "<html>captcha</html>"))


@pytest.mark.asyncio
async def test_non_200_is_an_error() -> None:
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="clck", sender=_sender(429, "https://clck.ru/abc"))


@pytest.mark.asyncio
async def test_no_time_left_fails_instead_of_hanging() -> None:
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="clck", timeout_ms=0, sender=_sender(200, "https://clck.ru/abc"))


@pytest.mark.asyncio
async def test_unknown_provider_is_refused() -> None:
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="tinyurl", sender=_sender(200, "https://tinyurl.com/abc"))


def test_tinyurl_is_deliberately_not_a_provider() -> None:
    """Замер: tinyurl.com УЖЕ в блок-листе поставщика.

    Через него приходит тот же отказ «disabled the target domain», что и
    напрямую, поэтому держать его в списке значило бы обещать работу,
    которой нет.
    """
    assert "tinyurl" not in PROVIDERS
    assert PROVIDERS[0] == "clck", "самый быстрый замер (9.1 с против 47 с) идёт первым"


@pytest.mark.asyncio
async def test_goo_needs_csrf_from_the_home_page() -> None:
    """Двухшаговый путь: главная отдаёт токен, затем POST."""
    calls: list = []

    async def send(url, *, timeout_ms, **kw):
        calls.append(url)
        if url.endswith("goo.su/"):
            return 200, '<meta name="csrf-token" content="' + "A" * 40 + '">'
        return 200, '{"short_url":"https://goo.su/U7xqGne"}'

    got = await shorten("https://www.ozon.ru/product/x/", provider="goo", sender=send)
    assert got == "https://goo.su/U7xqGne"
    assert calls[0].endswith("goo.su/")
    assert calls[1].endswith("/frontend-api/convert")


@pytest.mark.asyncio
async def test_goo_without_csrf_fails_loudly() -> None:
    with pytest.raises(ShortenFailed):
        await shorten("https://x/", provider="goo", sender=_sender(200, "<html>no token</html>"))
