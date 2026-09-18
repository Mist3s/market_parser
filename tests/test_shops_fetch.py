"""Егресс магазинов: прямой путь первым, один обход через пул или scrape.do."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from mktlink.shops.fetch import (
    STAGE_API,
    STAGE_DIRECT,
    STAGE_PROXY,
    ScrapeDoRoute,
    ShopFetcher,
    ShopUnreachable,
)
from mktlink.timing.deadline import Deadline, DeadlineExceeded
from tests.test_shops_service import Transport

URL = "https://moychay.ru/catalog/puer/shen_puer_pressovannyj/menhay-lao-2012"
PAGE = "<html><head><title>x</title></head><body><h1>Шен пуэр</h1></body></html>"
PROXY = "http://user:pw@proxy.test:8080"


class Provider:
    """Скриптованный обходной путь с журналом вызовов."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, url: str, *, timeout_ms: int) -> tuple[int, str, str]:
        self.calls.append({"url": url, "timeout_ms": timeout_ms})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


async def fetch(fetcher: ShopFetcher, budget_ms: int = 9_000):
    dl = Deadline.start(budget_ms, "t")
    got = await fetcher.fetch(dl, URL, cap_ms=budget_ms, reserve_ms=150)
    return got, [name for name, _ in dl.spent]


async def test_direct_success_needs_no_detour() -> None:
    provider = Provider((200, PAGE, URL))
    got, stages = await fetch(ShopFetcher(Transport((200, PAGE, URL)), provider=provider))
    assert (got.egress, got.blocked, got.status) == ("direct", False, 200)
    assert stages == [STAGE_DIRECT]
    assert provider.calls == []


async def test_silent_tls_goes_to_the_provider() -> None:
    provider = Provider((200, PAGE, URL))
    transport = Transport(ShopUnreachable("connect_timeout"))
    got, stages = await fetch(ShopFetcher(transport, provider=provider))
    assert (got.egress, got.status) == ("api", 200)
    assert stages == [STAGE_DIRECT, STAGE_API]
    assert provider.calls[0]["url"] == URL
    assert provider.calls[0]["timeout_ms"] > 5_000


async def test_direct_deadline_goes_to_the_provider_while_time_remains() -> None:
    provider = Provider((200, PAGE, URL))
    got, stages = await fetch(ShopFetcher(Transport(DeadlineExceeded("x")), provider=provider))
    assert got.egress == "api"
    assert stages == [STAGE_DIRECT, STAGE_API]


async def test_block_prefers_the_pool_proxy_over_the_provider() -> None:
    provider = Provider((200, PAGE, URL))
    transport = Transport((403, "", URL), (200, PAGE, URL))
    got, stages = await fetch(
        ShopFetcher(transport, proxy_for=lambda: PROXY, provider=provider)
    )
    assert (got.egress, got.blocked) == ("proxy", False)
    assert stages == [STAGE_DIRECT, STAGE_PROXY]
    assert [c["proxy"] for c in transport.calls] == [None, PROXY]
    assert provider.calls == []


async def test_block_without_a_pool_goes_to_the_provider() -> None:
    provider = Provider((200, PAGE, URL))
    got, stages = await fetch(ShopFetcher(Transport((429, "", URL)), provider=provider))
    assert (got.egress, got.blocked) == ("api", False)
    assert stages == [STAGE_DIRECT, STAGE_API]


async def test_block_on_the_detour_is_still_a_block() -> None:
    provider = Provider((403, "", URL))
    got, _ = await fetch(ShopFetcher(Transport((403, "", URL)), provider=provider))
    assert (got.egress, got.blocked, got.status) == ("api", True, 403)


async def test_without_any_detour_the_direct_failure_is_raised() -> None:
    with pytest.raises(ShopUnreachable, match="dns"):
        await fetch(ShopFetcher(Transport(ShopUnreachable("dns"))))


async def test_provider_failure_after_direct_failure_is_raised() -> None:
    provider = Provider(ShopUnreachable("provider: HTTP 429"))
    with pytest.raises(ShopUnreachable, match="provider"):
        await fetch(ShopFetcher(Transport(ShopUnreachable("connect_timeout")), provider=provider))


async def test_no_time_left_for_the_detour_is_a_deadline() -> None:
    provider = Provider((200, PAGE, URL))
    fetcher = ShopFetcher(Transport(ShopUnreachable("connect_timeout")), provider=provider)
    with pytest.raises(DeadlineExceeded):
        await fetch(fetcher, budget_ms=100)
    assert provider.calls == []


# --- маршрут через scrape.do ------------------------------------------------------------


async def test_provider_route_builds_the_cheapest_request() -> None:
    seen: dict = {}

    async def sender(req, *, headers, timeout_ms):
        seen.update(req=req, headers=headers, timeout_ms=timeout_ms)
        return 200, PAGE, URL + "/"

    status, body, final = await ScrapeDoRoute(token="t0k", sender=sender)(URL, timeout_ms=5_000)
    assert (status, body, final) == (200, PAGE, URL + "/")
    parts = urlsplit(seen["req"])
    assert parts.netloc == "api.scrape.do"
    assert dict(parse_qsl(parts.query)) == {"token": "t0k", "url": URL, "geoCode": "ru"}
    assert seen["headers"] == {"X-Rnet-Allow-Redirects": "1"}
    assert seen["timeout_ms"] == 5_000


async def test_provider_route_without_a_resolved_url_keeps_the_requested_one() -> None:
    async def sender(req, *, headers, timeout_ms):
        return 200, PAGE, None

    assert (await ScrapeDoRoute(token="t", sender=sender)(URL, timeout_ms=1_000))[2] == URL


async def test_provider_gate_is_unreachable_not_a_bad_link() -> None:
    async def sender(req, *, headers, timeout_ms):
        return 400, "We disabled the target domain", None

    with pytest.raises(ShopUnreachable, match="provider"):
        await ScrapeDoRoute(token="t", sender=sender)(URL, timeout_ms=1_000)


async def test_provider_passes_the_shop_status_through() -> None:
    async def sender(req, *, headers, timeout_ms):
        return 404, "<title>404</title>", URL

    status, _, _ = await ScrapeDoRoute(token="t", sender=sender)(URL, timeout_ms=1_000)
    assert status == 404
