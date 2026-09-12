"""Эндпоинт раскрутки, кэш раскрутки и два закрытых дефекта валидации."""

from __future__ import annotations

import pytest

from mktlink.api.apikey import COST
from mktlink.api.routes import Deps, Extraction, resolve
from mktlink.api.schemas import ResolveRequest
from mktlink.constants import RESOLVE_BUDGET_MS
from mktlink.marketplaces.verdict import Verdict
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db
from mktlink.store.unwound import UnwoundLinks

OZON_SHORT = "https://ozon.ru/t/8M3J7yH"
OZON_PDP = "https://www.ozon.ru/product/chay-3160461596/"
YM_SHORT = "https://market.yandex.ru/cc/AxQZZh"
YM_PDP = "https://market.yandex.ru/card/da-hun-pao/101814267477"


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "r.sqlite")
    c = connect(tmp_path / "r.sqlite")
    yield c
    c.close()


async def _never(dl, c, budget_ms):
    raise AssertionError("лестница не должна вызываться при раскрутке")


def _deps(conn, *, resolver=None, unwound=None) -> Deps:
    return Deps(
        cache=ProductCache(conn),
        ladder=_never,
        resolver=resolver,
        unwound=unwound,
    )


def _resolver(target: str, hops: int = 1):
    calls: list[str] = []

    async def r(dl, url, mp):
        calls.append(url)
        return target, hops

    r.calls = calls  # type: ignore[attr-defined]
    return r


# --- сам эндпоинт --------------------------------------------------------------


async def test_short_link_becomes_canonical(conn) -> None:
    code, body = await resolve(
        ResolveRequest(url=OZON_SHORT), _deps(conn, resolver=_resolver(OZON_PDP, 2))
    )
    assert code == 200
    assert body.status == "ok"
    assert body.url.submitted == OZON_SHORT
    assert body.url.canonical == "https://www.ozon.ru/product/chay-3160461596/"
    assert body.url.hops == 2
    assert body.marketplace == "ozon"
    assert body.product.id == "3160461596"


async def test_extraction_fields_are_empty_and_that_is_honest(conn) -> None:
    """Мы не смотрели карточку, поэтому имени и продавца здесь нет.

    ``meta.source = "resolve"`` — единственное, что отличает «не искали» от
    «не нашли». Без него клиент прочитал бы пустого продавца как отказ.
    """
    _, body = await resolve(
        ResolveRequest(url=OZON_SHORT), _deps(conn, resolver=_resolver(OZON_PDP))
    )
    assert body.meta.source == "resolve"
    assert body.product.name is None
    assert body.seller.name is None
    assert body.seller.unverified_name is None


async def test_already_canonical_url_is_returned_as_is(conn) -> None:
    """Нормальную ссылку эндпоинт тоже принимает: он нормализует, а не только разворачивает."""
    code, body = await resolve(ResolveRequest(url=YM_PDP), _deps(conn))
    assert code == 200
    assert body.url.hops == 0
    assert body.url.canonical is not None
    assert body.marketplace == "ym"


async def test_ladder_is_never_touched(conn) -> None:
    """Раскрутка не стоит ни кредита поставщика, ни слота прокси.

    ``_never`` падает при вызове, поэтому тест доказывает это исполнением, а
    не чтением кода.
    """
    code, _ = await resolve(ResolveRequest(url=YM_PDP), _deps(conn))
    assert code == 200


async def test_budget_allows_provider_rendering(conn) -> None:
    _, body = await resolve(ResolveRequest(url=YM_PDP), _deps(conn))
    assert body.meta.budget_ms == RESOLVE_BUDGET_MS
    assert RESOLVE_BUDGET_MS == 30_000


async def test_bad_url_is_rejected_the_same_way_as_on_the_card(conn) -> None:
    code, body = await resolve(ResolveRequest(url="http://169.254.1.1/x"), _deps(conn))
    assert code == 422
    assert body.status in ("invalid_url", "host_not_allowed")


async def test_foreign_marketplace_is_refused(conn) -> None:
    code, body = await resolve(
        ResolveRequest(url="https://www.detmir.ru/product/index/id/123/"), _deps(conn)
    )
    assert code == 422
    assert body.status == "host_not_allowed"


async def test_unwind_landing_on_a_listing_is_not_a_product(conn) -> None:
    code, body = await resolve(
        ResolveRequest(url=OZON_SHORT),
        _deps(conn, resolver=_resolver("https://www.ozon.ru/category/chay-9373/")),
    )
    assert code == 422
    assert body.status == "not_a_product_url"


# --- кэш раскрутки ---------------------------------------------------------------


async def test_unwind_is_paid_once(conn) -> None:
    """Второй запрос той же ссылки не делает ни одного редиректа.

    Экономия здесь — целые сетевые хопы из бюджета, а не микросекунды: именно
    поэтому таблица ``shortlink`` и существовала, хотя ею никто не пользовался.
    """
    store = UnwoundLinks(conn)
    res = _resolver(OZON_PDP, 3)
    deps = _deps(conn, resolver=res, unwound=store)

    _, first = await resolve(ResolveRequest(url=OZON_SHORT), deps)
    assert len(res.calls) == 1
    assert first.url.hops == 3
    # Первый вызов ОБЯЗАН быть промахом. Регрессия: признак попадания
    # выводился из числа хопов, и живой первый ответ отрапортовал «hit» на
    # пустом кэше — метрика попаданий врала бы с самого начала.
    assert first.meta.cache == "miss"

    _, second = await resolve(ResolveRequest(url=OZON_SHORT), deps)
    assert len(res.calls) == 1, "раскрутка обязана быть оплачена один раз"
    assert second.url.canonical == first.url.canonical
    assert second.url.hops == 3, "число хопов сохраняется — это свойство ссылки"
    assert second.meta.cache == "hit"


async def test_cache_survives_a_new_store_instance(conn) -> None:
    UnwoundLinks(conn).put(OZON_SHORT, OZON_PDP, 2)
    got = UnwoundLinks(conn).get(OZON_SHORT)
    assert got is not None
    assert (got.canonical, got.hops) == (OZON_PDP, 2)


async def test_unknown_link_is_a_miss(conn) -> None:
    assert UnwoundLinks(conn).get("https://ozon.ru/t/NEVERSEEN") is None


async def test_the_main_endpoint_uses_the_same_cache(conn) -> None:
    """Кэш общий: развернув ссылку через /v1/resolve, карточка платит 0 хопов."""
    from mktlink.api.routes import handle
    from mktlink.api.schemas import ProductRequest

    store = UnwoundLinks(conn)
    res = _resolver(OZON_PDP, 2)

    async def ladder(dl, c, budget_ms):
        return Extraction(verdict=Verdict.OK, name="Чай", seller_name="Шуняня")

    deps = Deps(cache=ProductCache(conn), ladder=ladder, resolver=res, unwound=store)
    await resolve(ResolveRequest(url=OZON_SHORT), deps)
    assert len(res.calls) == 1

    code, body = await handle(ProductRequest(url=OZON_SHORT), deps)
    assert code == 200
    assert len(res.calls) == 1, "карточка не должна раскручивать заново"
    assert body.url.hops == 2


# --- цена в кредитах -------------------------------------------------------------


def test_unwind_costs_less_than_a_card() -> None:
    """Цена отражает то, что операция тратит, иначе лимит наказывает за экономию."""
    assert COST["redirect_unwind"] < COST["cold"]
    assert COST["redirect_unwind"] == 2
    assert COST["cache_hit"] < COST["redirect_unwind"]


# --- дефекты, найденные состязательным разбором ---------------------------------


def test_unwind_key_ignores_spelling_and_tracking() -> None:
    """D2. Четыре написания одной ссылки давали четыре раскрутки.

    Самое частое из них — с utm-меткой: ссылку пересылают из мессенджера, и
    он её дописывает. То есть кэш не попадал именно в основном случае.
    """
    from mktlink.store.unwound import normalise

    spellings = [
        "https://ozon.ru/t/AbC123",
        "https://www.ozon.ru/t/AbC123",
        "https://ozon.ru/t/AbC123/",
        "https://ozon.ru/t/AbC123?utm_source=tg",
        "https://OZON.RU/t/AbC123#frag",
    ]
    assert len({normalise(s) for s in spellings}) == 1


def test_different_codes_stay_different() -> None:
    """Нормализация не имеет права склеивать РАЗНЫЕ ссылки."""
    from mktlink.store.unwound import normalise

    assert normalise("https://ozon.ru/t/AAA111") != normalise("https://ozon.ru/t/BBB222")
    # И разные маркетплейсы тоже.
    assert normalise("https://ozon.ru/t/X1234") != normalise("https://market.yandex.ru/cc/X1234")


async def test_cache_hits_across_spellings(conn) -> None:
    store = UnwoundLinks(conn)
    res = _resolver(OZON_PDP, 2)
    deps = _deps(conn, resolver=res, unwound=store)

    await resolve(ResolveRequest(url="https://ozon.ru/t/AbC123"), deps)
    await resolve(ResolveRequest(url="https://www.ozon.ru/t/AbC123?utm_source=tg"), deps)
    assert len(res.calls) == 1, "второе написание обязано попасть в кэш"


async def test_seller_status_says_we_did_not_look(conn) -> None:
    """D3. Дефолт нёс `unknown_layout` — «смотрели и не разобрались»."""
    from mktlink.marketplaces.verdict import SellerStatus

    _, body = await resolve(ResolveRequest(url=YM_PDP), _deps(conn))
    assert body.seller.status == str(SellerStatus.NOT_REQUESTED)
    assert body.seller.status != "unknown_layout"


async def test_broken_redirect_chain_is_422_not_500(conn) -> None:
    """D4. Четыре исключения раскрутки улетали в общий обработчик.

    Для эндпоинта, который разворачивает ссылки, сломанная цепочка — штатный
    вход, а не внутренняя ошибка сервиса.
    """
    from mktlink.urls.redirects import (
        CrossedMarketplace,
        NotARedirect,
        RedirectLoop,
        TooManyHops,
    )

    for exc in (
        TooManyHops("https://ozon.ru/t/x", 4),
        RedirectLoop("loop"),
        CrossedMarketplace("ozon -> ym"),
        NotARedirect("no location"),
    ):
        async def failing(dl, url, mp, _e=exc):
            raise _e

        code, body = await resolve(
            ResolveRequest(url=OZON_SHORT), _deps(conn, resolver=failing)
        )
        assert code == 422, type(exc).__name__
        assert body.status == "not_a_product_url"
        assert body.meta.detail["unwind"] == type(exc).__name__


async def test_ssrf_during_unwind_is_an_input_error(conn) -> None:
    from mktlink.urls.ssrf import SsrfRejected

    async def failing(dl, url, mp):
        raise SsrfRejected("private address", "10.0.0.1")

    code, body = await resolve(ResolveRequest(url=OZON_SHORT), _deps(conn, resolver=failing))
    assert code == 422
    assert body.status == "host_not_allowed"
