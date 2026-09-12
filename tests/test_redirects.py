"""Раскрутка: заключение в маркетплейс, veto на челлендж, циклы, лимит хопов."""

from __future__ import annotations

import asyncio
import time

import pytest

from mktlink.constants import REDIRECT_HOPS_MAX
from mktlink.timing.deadline import Deadline, DeadlineExceeded
from mktlink.urls.redirects import (
    CrossedMarketplace,
    NotARedirect,
    RedirectLoop,
    RedirectResolver,
    TooManyHops,
    shortlink_key,
)
from mktlink.urls.ssrf import SsrfRejected, UnwindChallenged

OZON_SHORT = "https://ozon.ru/t/AbC123"
OZON_PDP = "https://www.ozon.ru/product/smes-1234567890/"
YM_SHORT = "https://market.yandex.ru/cc/AbC1_2-3"
YM_PDP = "https://market.yandex.ru/card/slug/4382957723"


def chain(*pairs: tuple[int, str | None]):
    """Заготовленная цепочка ответов на хопы."""
    seq = list(pairs)

    async def fetch(url: str, timeout_ms: int) -> tuple[int, str | None]:
        return seq.pop(0) if seq else (200, None)

    return fetch


def dns_ok():
    async def resolve(host: str) -> list[str]:
        return ["93.158.134.3"]

    return resolve


async def test_a_single_hop_short_link_resolves() -> None:
    r = RedirectResolver(chain((302, OZON_PDP)), dns_ok())
    url, hops = await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")
    assert url == OZON_PDP and hops == 1


async def test_three_hops_are_allowed_and_four_are_not() -> None:
    """Потолок цепочки: ozon.ru/t/<code> -> /product/<id> -> www/product/<slug>-<id>."""
    ok = RedirectResolver(
        chain(
            (302, "https://ozon.ru/t/Second"),
            (302, "https://ozon.ru/t/Third"),
            (302, OZON_PDP),
        ),
        dns_ok(),
    )
    url, hops = await ok(Deadline.start(15000, "t"), OZON_SHORT, "ozon")
    assert url == OZON_PDP and hops == 3 == REDIRECT_HOPS_MAX

    endless = RedirectResolver(
        chain(*[(302, f"https://ozon.ru/t/Hop{i}") for i in range(10)]), dns_ok()
    )
    with pytest.raises(TooManyHops):
        await endless(Deadline.start(15000, "t"), OZON_SHORT, "ozon")


async def test_a_loop_is_caught_by_the_visited_set() -> None:
    r = RedirectResolver(chain((302, OZON_SHORT), (302, OZON_SHORT)), dns_ok())
    with pytest.raises(RedirectLoop):
        await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")


async def test_a_hop_into_another_marketplace_is_refused() -> None:
    """Заключение фиксируется на хопе 0 и не меняется."""
    r = RedirectResolver(chain((302, YM_PDP)), dns_ok())
    with pytest.raises(CrossedMarketplace):
        await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")


async def test_a_hop_outside_the_allowlist_is_refused() -> None:
    r = RedirectResolver(chain((302, "https://evil.example/x")), dns_ok())
    with pytest.raises(CrossedMarketplace):
        await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")


async def test_the_yandex_challenge_on_the_same_host_is_caught() -> None:
    """Хост совпадает, правило заключения проходит — ловит только путевой veto."""
    r = RedirectResolver(
        chain((302, "https://market.yandex.ru/showcaptcha?cc=1&retpath=x")), dns_ok()
    )
    with pytest.raises(UnwindChallenged) as ei:
        await r(Deadline.start(15000, "t"), YM_SHORT, "ym")
    assert ei.value.egress == "direct", "наблюдение прямого егресса, прокси ни при чём"


async def test_every_hop_is_dns_checked_not_only_the_first() -> None:
    seen: list[str] = []

    async def resolve(host: str) -> list[str]:
        seen.append(host)
        # Второй хост резолвится в приватную сеть.
        return ["93.158.134.3"] if not seen[1:] else ["93.158.134.3", "10.0.0.1"]

    r = RedirectResolver(chain((302, "https://ozon.ru/t/Second"), (302, OZON_PDP)), resolve)
    with pytest.raises(SsrfRejected):
        await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")
    assert len(seen) == 2, "проверен не только первый хоп"


async def test_a_non_redirect_answer_is_not_silently_accepted() -> None:
    r = RedirectResolver(chain((200, None)), dns_ok())
    with pytest.raises(NotARedirect):
        await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")


async def test_a_canonical_url_needs_no_unwinding() -> None:
    async def never(url: str, timeout_ms: int):
        raise AssertionError("не должно быть ни одного хопа")

    r = RedirectResolver(never, dns_ok())
    url, hops = await r(Deadline.start(15000, "t"), OZON_PDP, "ozon")
    assert url == OZON_PDP and hops == 0


async def test_relative_location_is_resolved_against_the_current_url() -> None:
    r = RedirectResolver(chain((302, "/product/smes-1234567890/")), dns_ok())
    url, _ = await r(Deadline.start(15000, "t"), OZON_SHORT, "ozon")
    assert url == "https://ozon.ru/product/smes-1234567890/"


async def test_hops_are_budget_stages() -> None:
    dl = Deadline.start(15000, "t")
    r = RedirectResolver(chain((302, OZON_PDP)), dns_ok())
    await r(dl, OZON_SHORT, "ozon")
    assert "unwind0" in dl.ledger()


async def test_shortlink_key_is_stable_and_distinct() -> None:
    assert shortlink_key(OZON_SHORT) == shortlink_key(OZON_SHORT)
    assert shortlink_key(OZON_SHORT) != shortlink_key(YM_SHORT)


async def test_one_slow_hop_can_use_the_remaining_resolve_budget():
    async def slow(url, timeout_ms):
        await asyncio.sleep(0.65)
        return 302, OZON_PDP

    resolved, hops = await RedirectResolver(slow, dns_ok())(
        Deadline.start(3900, "slow-hop"), OZON_SHORT, "ozon")
    assert (resolved, hops) == (OZON_PDP, 1)


async def test_dns_is_inside_the_request_deadline():
    async def slow_dns(host):
        await asyncio.sleep(0.3)
        return ["93.158.134.3"]

    dl = Deadline.start(30, "slow-dns")
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await RedirectResolver(chain((302, OZON_PDP)), slow_dns)(dl, OZON_SHORT, "ozon")
    assert time.monotonic() - started < 0.2


async def test_direct_403_is_a_challenge_not_a_bad_product_url():
    with pytest.raises(UnwindChallenged):
        await RedirectResolver(chain((403, None)), dns_ok())(
            Deadline.start(3900, "blocked"), OZON_SHORT, "ozon")
