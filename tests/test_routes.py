"""Оркестратор пути запроса, целиком, без сети.

Лестница и раскрутка инъектируются, поэтому проверяется именно контракт:
какой статус, какое тело, и — главное — что канонический URL присутствует
везде, где он известен.
"""

from __future__ import annotations

import pytest

from mktlink.api.routes import Deps, Extraction, handle
from mktlink.api.schemas import ProductRequest
from mktlink.constants import BUDGET_FLOOR_MS, RETRY_AFTER_FETCH_S, RETRY_AFTER_MINT_S
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.store.cache import FRESH_TTL_S, ProductCache
from mktlink.store.db import connect as db_connect
from mktlink.store.db import init_db
from mktlink.timing.deadline import DeadlineExceeded
from mktlink.urls.ssrf import UnwindChallenged

OZON_PDP = "https://www.ozon.ru/product/smes-nutrilon-1234567890/"
YM_PDP = "https://market.yandex.ru/card/pyure-semper/4382957723"
OZON_SHORT = "https://ozon.ru/t/AbC123"

GOOD = Extraction(
    verdict=Verdict.OK,
    name="Пюре Semper 4 овоща",
    seller_name="ООО «Ромашка»",
    seller_id="12345",
    seller_status=SellerStatus.RESOLVED,
    seller_source="ym:state:widgets.DefaultOffer.shop.name",
    rung="Y1",
    confirmations=1,
)


def deps(ladder=None, resolver=None, cache=None, budget=15000) -> Deps:
    async def ok_ladder(dl, c, budget_ms):
        return GOOD

    return Deps(
        cache=cache or ProductCache(),
        ladder=ladder or ok_ladder,
        resolver=resolver,
        budget_ms=budget,
    )


async def call(url: str, d: Deps, **kw):
    return await handle(ProductRequest(url=url, **kw), d)


# --- счастливый путь ----------------------------------------------------------


async def test_resolved_seller_is_returned_with_its_provenance() -> None:
    code, r = await call(YM_PDP, deps())
    assert code == 200 and r.status == "ok"
    assert r.marketplace == "ym"
    assert r.product.name == "Пюре Semper 4 овоща"
    assert r.seller.name == "ООО «Ромашка»"
    assert r.seller.kind == "third_party"
    assert r.seller.source is not None, "провенанс обязателен"
    assert r.url.canonical == YM_PDP


async def test_both_id_spaces_are_echoed_so_the_caller_can_pin_the_offer() -> None:
    _, r = await call(YM_PDP, deps())
    assert r.product.sku_id == "4382957723"
    assert r.product.id_kind == "sku_id"


async def test_model_url_is_marked_as_an_unstable_snapshot() -> None:
    """Честное «это снимок аукциона, а не свойство ссылки»."""
    _, r = await call(YM_PDP, deps())
    assert r.offer.selection == "marketplace_default"
    assert r.offer.stable is False

    _, pinned = await call(YM_PDP + "?sku=99", deps())
    assert pinned.offer.selection == "explicit"
    assert pinned.offer.stable is True


async def test_ledger_travels_in_the_response_not_only_in_logs() -> None:
    _, r = await call(YM_PDP, deps())
    assert r.meta.budget_ms == 15000
    assert isinstance(r.meta.ledger, list)


# --- продавец никогда не догадка ------------------------------------------------


async def test_unresolved_seller_is_null_and_the_response_says_why() -> None:
    async def partial(dl, c, budget_ms):
        return Extraction(
            verdict=Verdict.PARTIAL,
            name="Пюре Semper",
            seller_name="Semper",  # это бренд, а не продавец
            seller_status=SellerStatus.UNKNOWN_LAYOUT,
            rung="Y1",
        )

    code, r = await call(YM_PDP, deps(ladder=partial))
    assert code == 200 and r.status == "ok"
    assert r.product.name == "Пюре Semper", "имя отдаём"
    assert r.seller.name is None, "а продавца — нет, потому что он не разрешён"
    assert r.seller.status == "unknown_layout"
    assert r.seller.kind == "unknown"
    assert r.meta.degraded is True


async def test_marketplace_as_seller_is_distinguishable_from_failure() -> None:
    async def first_party(dl, c, budget_ms):
        return Extraction(
            verdict=Verdict.OK,
            name="Товар",
            seller_name="Ozon",
            seller_status=SellerStatus.FIRST_PARTY,
            seller_source="ozon:state:widgetStates.webCurrentSeller.name",
            rung="O1",
        )

    _, r = await call(OZON_PDP, deps(ladder=first_party))
    assert r.seller.name == "Ozon" and r.seller.kind == "first_party"
    assert r.seller.status == "first_party"


# --- отбраковка ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("https://bit.ly/abc123", "host_not_allowed"),
        ("https://www.detmir.ru/product/index/id/123", "host_not_allowed"),
        ("https://www.ozon.ru/category/detskoe-pitanie-7030/", "not_a_product_url"),
        ("http://www.ozon.ru/product/1234567890", "invalid_url"),
        ("https://127.0.0.1/product/1234567890", "invalid_url"),
    ],
)
async def test_bad_input_is_refused_cheaply(url: str, status: str) -> None:
    code, r = await call(url, deps())
    assert r.status == status
    assert code == 422
    assert r.url.submitted == url


async def test_out_of_scope_marketplace_costs_no_proxy_and_no_budget() -> None:
    """Самый частый отказ при трёх маркетплейсах, а не экзотика."""
    called = False

    async def never(dl, c, budget_ms):
        nonlocal called
        called = True
        return GOOD

    code, r = await call("https://samokat.ru/product/123", deps(ladder=never))
    assert code == 422 and r.status == "host_not_allowed"
    assert not called, "лестница не должна быть тронута"


async def test_offsite_challenge_is_reported_as_a_challenge_not_a_bad_link() -> None:
    """Иначе клиент идёт чинить свой корректный URL."""
    code, r = await call("https://yandex.ru/showcaptcha?cc=1", deps())
    assert r.status == "unwind_challenged"
    assert code == 504


async def test_budget_below_the_floor_is_refused_with_the_floor_echoed() -> None:
    code, r = await call(YM_PDP, deps(), max_wait_ms=BUDGET_FLOOR_MS)
    assert code == 200, "ровно на полу — ещё можно"

    with pytest.raises(ValueError):
        # Ниже пола pydantic не пропускает: обещать работу, которой не будет,
        # нельзя уже на уровне схемы.
        ProductRequest(url=YM_PDP, max_wait_ms=BUDGET_FLOOR_MS - 1)


async def test_extra_field_is_a_typo_not_an_extension() -> None:
    with pytest.raises(ValueError):
        ProductRequest(url=YM_PDP, unknown_flag=True)


# --- короткие ссылки --------------------------------------------------------------


async def test_short_link_is_unwound_and_the_real_url_is_returned() -> None:
    """Требование заказчика целиком, от входа до ответа."""

    async def resolver(dl, url, mp):
        return OZON_PDP, 2

    code, r = await call(OZON_SHORT, deps(resolver=resolver))
    assert code == 200
    assert r.url.submitted == OZON_SHORT
    assert r.url.canonical == OZON_PDP
    assert r.url.hops == 2


async def test_challenge_during_unwind_never_blames_the_link() -> None:
    async def challenged(dl, url, mp):
        raise UnwindChallenged(url, egress="direct")

    code, r = await call(OZON_SHORT, deps(resolver=challenged))
    assert code == 504 and r.status == "unwind_challenged"


async def test_the_only_504_without_a_canonical_url_is_an_unresolved_chain() -> None:
    async def too_slow(dl, url, mp):
        raise DeadlineExceeded("unwind")

    code, r = await call(OZON_SHORT, deps(resolver=too_slow))
    assert code == 504 and r.status == "deadline_exceeded"
    assert r.url.canonical is None, "именно этот случай — единственный"


# --- деградация ---------------------------------------------------------------------


async def test_pending_still_carries_the_canonical_url() -> None:
    """Свойство, ради которого 202 вообще полезен клиенту."""

    async def blocked(dl, c, budget_ms):
        return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")

    code, r = await call(YM_PDP, deps(ladder=blocked))
    assert code == 202 and r.status == "pending"
    assert r.url.canonical == YM_PDP
    assert r.meta.retry_after_seconds == RETRY_AFTER_MINT_S


async def test_retry_after_is_derived_from_the_work_being_waited_on() -> None:
    async def slow(dl, c, budget_ms):
        raise DeadlineExceeded("Y1")

    _, r = await call(YM_PDP, deps(ladder=slow))
    assert r.status == "pending"
    assert r.meta.retry_after_seconds == RETRY_AFTER_FETCH_S, "jar тёплый — ждать нечего долго"


async def test_stale_is_offered_only_with_permission_and_always_with_its_age(tmp_path) -> None:
    """Пол для stale живёт в SQLite, а не в памяти — это его смысл.

    Кэш без соединения устаревшее отдать не может по устройству: in-proc LRU
    делает повтор после 202 бесплатным, а пережить рестарт и дать старый
    правдивый ответ вместо никакого — работа файла.
    """
    init_db(tmp_path / "c.sqlite")
    conn = db_connect(tmp_path / "c.sqlite")
    cache = ProductCache(conn)
    key = "pl:v1:ym:s4382957723@*"
    cache.put(
        key,
        {"name": "Старое имя", "seller_status": "resolved", "seller_name": "ООО «Ромашка»"},
    )
    # Состарим запись за пределы свежести, но в пределах разрешённого клиентом.
    conn.execute(
        "UPDATE product SET fetched_at = unixepoch() - ? WHERE cache_key = ?",
        (FRESH_TTL_S + 60, key),
    )
    cache._mem.clear()  # noqa: SLF001 — иначе проверим память, а не пол

    async def blocked(dl, c, budget_ms):
        return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")

    code, r = await call(YM_PDP, deps(ladder=blocked, cache=cache), max_stale_s=3600)
    assert code == 200 and r.status == "stale"
    assert r.meta.degraded is True
    assert r.meta.detail["age_s"] >= FRESH_TTL_S

    code, r = await call(YM_PDP, deps(ladder=blocked, cache=cache), max_stale_s=0)
    assert code == 202, "без разрешения устаревшее не отдаём"
    conn.close()


async def test_not_found_needs_two_confirmations_before_it_is_a_404() -> None:
    """Молчаливый ноль — форма блока, а не форма отсутствия товара."""

    async def one(dl, c, budget_ms):
        return Extraction(verdict=Verdict.NOT_FOUND, confirmations=1)

    code, r = await call(YM_PDP, deps(ladder=one))
    assert code == 200 and r.status == "not_found_unconfirmed"

    async def two(dl, c, budget_ms):
        return Extraction(verdict=Verdict.NOT_FOUND, confirmations=2)

    code, r = await call(YM_PDP, deps(ladder=two))
    assert code == 404 and r.status == "not_found"


# --- кэш ------------------------------------------------------------------------------


async def test_second_call_is_served_from_cache() -> None:
    calls = 0

    async def counting(dl, c, budget_ms):
        nonlocal calls
        calls += 1
        return GOOD

    d = deps(ladder=counting)
    await call(YM_PDP, d)
    _, r = await call(YM_PDP, d)
    assert calls == 1
    assert r.meta.cache == "hit"


async def test_different_offers_of_one_card_do_not_share_a_cache_entry() -> None:
    calls = 0

    async def counting(dl, c, budget_ms):
        nonlocal calls
        calls += 1
        return GOOD

    d = deps(ladder=counting)
    await call(YM_PDP + "?sku=1", d)
    await call(YM_PDP + "?sku=2", d)
    assert calls == 2, "иначе вернули бы продавца другого оффера"
