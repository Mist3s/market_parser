"""Транспорт через скрейпинг-API: параметры, атрибуция, перевод отказов.

Тесты сети не касаются: транспорт принимает ``sender``, и все замеренные
формы ответов поставщика воспроизводятся как строки.
"""

from __future__ import annotations

import pathlib

import pytest

from mktlink.budget import API_LADDER, BudgetTooSmall, plan, request_ladder
from mktlink.egress.client import EgressClient
from mktlink.egress.scrapedo import (
    API_EGRESS_ID,
    CREDITS,
    PARAMS,
    SHORTEN_CAP_MS,
    SHORTEN_REQUIRED,
    DomainDisabled,
    ScrapeDoError,
    ScrapeDoTransport,
    api_url,
    marketplace_of,
)
from mktlink.marketplaces import ozon
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.timing.deadline import DeadlineExceeded

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: Замеренные формы отказов поставщика, дословно.
DISABLED = (
    '{"URL":"https://www.ozon.ru/product/x/","StatusCode":400,'
    '"Message":["We disabled the target domain for free packages.'
    ' Please upgrade your account or contact with us for trial."]}'
)
REDIRECT_ERROR = (
    '{"error": "Redirect error: is_redirect error: wreq::Error { kind: Redirect,'
    ' uri: https://www.ozon.ru/product/x/?__rr=1, source: TooManyRedirects }",'
    ' "emulation": "Safari18_3", "status": 310}'
)


def _sender(status: int, body: str, seen: list | None = None):
    async def send(url, *, headers, timeout_ms):
        if seen is not None:
            seen.append({"url": url, "headers": headers, "timeout_ms": timeout_ms})
        return status, body

    return send


def _shortener(short: str, seen: list | None = None):
    async def send(url, *, timeout_ms, **kw):
        if seen is not None:
            seen.append({"url": url, "timeout_ms": timeout_ms})
        return 200, short

    return send


# --- маршрутизация по хосту ---------------------------------------------------


def test_host_allowlist_is_closed() -> None:
    """Чужой хост во внешний сервис не уходит вовсе."""
    assert marketplace_of("https://www.ozon.ru/product/x/") == "ozon"
    assert marketplace_of("https://market.yandex.ru/card/x/1") == "ym"
    assert marketplace_of("https://evil.example/") is None
    # Поддомен не наследует разрешение: это та же дисциплина, что в SSRF.
    assert marketplace_of("https://ozon.ru.evil.example/") is None


@pytest.mark.asyncio
async def test_unknown_host_is_refused_before_any_request() -> None:
    seen: list = []
    t = ScrapeDoTransport(token="T", sender=_sender(200, "x", seen))
    with pytest.raises(ScrapeDoError):
        await t("https://evil.example/", headers={}, proxy=None, impersonate=None, timeout_ms=1000)
    assert seen == [], "до проверки хоста запрос уходить не должен"


# --- параметры, замеренные -----------------------------------------------------


def test_render_is_on_for_ozon_and_off_for_ym() -> None:
    """Замер: рендеринг нужен Ozon и ЛОМАЕТ Я.Маркет (капча вместо карточки)."""
    assert PARAMS["ozon"]["render"] == "true"
    assert "render" not in PARAMS["ym"]


def test_geo_is_russia_everywhere() -> None:
    assert all(p["geoCode"] == "ru" for p in PARAMS.values())
    assert all(p["super"] == "true" for p in PARAMS.values())


def test_only_ozon_needs_shortening() -> None:
    """Замер: market.yandex.ru через сокращалку отдаёт капчу, напрямую — карточку."""
    assert SHORTEN_REQUIRED == {"ozon"}


def test_api_url_carries_token_and_params() -> None:
    url = api_url("https://x/?a=b", token="TT", marketplace="ym")
    assert url.startswith("https://api.scrape.do/?token=TT&url=")
    # Целевой URL закодирован целиком: иначе его query склеится с нашим.
    assert "https%3A%2F%2Fx%2F%3Fa%3Db" in url
    assert "geoCode=ru" in url and "super=true" in url


def test_credits_are_recorded_for_every_lane() -> None:
    """Цена ступени — часть контракта: бесплатный тариф даёт 1000 в месяц."""
    assert set(CREDITS) >= set(PARAMS)
    assert CREDITS["ozon"] > CREDITS["ym"], "рендеринг у поставщика стоит дороже"


# --- сокращение ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ozon_goes_through_the_shortener_and_ym_does_not() -> None:
    seen_req: list = []
    seen_short: list = []
    t = ScrapeDoTransport(
        token="T",
        shorten_via="clck",
        sender=_sender(200, "body", seen_req),
        shorten_sender=_shortener("https://clck.ru/abc123", seen_short),
    )
    kw = dict(headers={}, proxy=None, impersonate=None, timeout_ms=20_000)

    await t("https://www.ozon.ru/product/x/", **kw)
    assert len(seen_short) == 1
    assert "clck.ru%2Fabc123" in seen_req[-1]["url"]

    await t("https://market.yandex.ru/card/x/1", **kw)
    assert len(seen_short) == 1, "Я.Маркет сокращать нельзя — замер"
    assert "market.yandex.ru" in seen_req[-1]["url"]


@pytest.mark.asyncio
async def test_shortening_takes_a_capped_slice_not_a_share() -> None:
    """Регрессия: доля 25 % отнимала 3.4 с и Ozon не успевал."""
    seen_req: list = []
    seen_short: list = []
    t = ScrapeDoTransport(
        token="T",
        shorten_via="clck",
        sender=_sender(200, "b", seen_req),
        shorten_sender=_shortener("https://clck.ru/abc123", seen_short),
    )
    await t(
        "https://www.ozon.ru/product/x/",
        headers={}, proxy=None, impersonate=None, timeout_ms=13_500,
    )
    assert seen_short[0]["timeout_ms"] == SHORTEN_CAP_MS
    # Запросу осталось всё остальное, а не три четверти.
    assert seen_req[0]["timeout_ms"] == 13_500 - SHORTEN_CAP_MS


@pytest.mark.asyncio
async def test_tiny_budget_never_lets_shortening_eat_everything() -> None:
    seen_req: list = []
    t = ScrapeDoTransport(
        token="T",
        shorten_via="clck",
        sender=_sender(200, "b", seen_req),
        shorten_sender=_shortener("https://clck.ru/abc123"),
    )
    await t(
        "https://www.ozon.ru/product/x/",
        headers={}, proxy=None, impersonate=None, timeout_ms=200,
    )
    assert seen_req[0]["timeout_ms"] >= 100, "хотя бы половина остаётся запросу"


@pytest.mark.asyncio
async def test_failed_required_shortening_does_not_send_the_direct_url() -> None:
    async def broken(url, *, timeout_ms, **kw):
        return 200, "Error, database insert failed"

    seen = []
    t = ScrapeDoTransport(
        token="T", shorten_via="clck", sender=_sender(400, DISABLED, seen), shorten_sender=broken
    )
    with pytest.raises(ScrapeDoError) as error:
        await t(
            "https://www.ozon.ru/product/x/",
            headers={}, proxy=None, impersonate=None, timeout_ms=5_000,
        )
    assert error.value.reason == "shortener_unavailable"
    assert seen == []


# --- перевод отказов поставщика -------------------------------------------------


@pytest.mark.asyncio
async def test_domain_gate_is_its_own_error_not_a_bad_request() -> None:
    """400 «disabled the target domain» — это тариф, а не плохая ссылка."""
    t = ScrapeDoTransport(token="T", sender=_sender(400, DISABLED))
    with pytest.raises(DomainDisabled):
        await t(
            "https://www.ozon.ru/product/x/",
            headers={}, proxy=None, impersonate=None, timeout_ms=5_000,
        )


@pytest.mark.asyncio
async def test_redirect_stop_is_a_provider_error() -> None:
    t = ScrapeDoTransport(token="T", sender=_sender(200, REDIRECT_ERROR))
    with pytest.raises(ScrapeDoError):
        await t(
            "https://www.ozon.ru/product/x/",
            headers={}, proxy=None, impersonate=None, timeout_ms=5_000,
        )


@pytest.mark.asyncio
async def test_allow_redirects_header_is_always_sent() -> None:
    """Без него цепочка Ozon обрывается их ошибкой 310 — замерено."""
    seen: list = []
    t = ScrapeDoTransport(token="T", sender=_sender(200, "b", seen))
    await t(
        "https://market.yandex.ru/card/x/1",
        headers={}, proxy=None, impersonate=None, timeout_ms=5_000,
    )
    assert seen[0]["headers"]["X-Rnet-Allow-Redirects"] == "1"


@pytest.mark.asyncio
async def test_marketplace_body_passes_through_untouched() -> None:
    html = (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")
    t = ScrapeDoTransport(token="T", sender=_sender(200, html))
    status, body = await t(
        "https://www.ozon.ru/product/x/",
        headers={}, proxy=None, impersonate=None, timeout_ms=5_000,
    )
    assert status == 200
    r = ozon.parse_pdp_html(body, anchor_ids={"3160461596"})
    assert r.verdict is Verdict.OK
    assert r.seller_name == "Шуняня"
    assert r.seller_id == "shunana"
    assert r.seller_status is SellerStatus.RESOLVED


# --- атрибуция егресса ----------------------------------------------------------


def test_transport_declares_a_foreign_egress() -> None:
    """Адрес не наш, и клиент обязан это знать: от этого зависят деньги."""
    t = ScrapeDoTransport(token="T")
    assert t.egress_kind == "api"
    assert EgressClient(t).egress_kind(None) == "api"
    # И через прокси-URL тоже 'api': транспорт знает лучше.
    assert EgressClient(t).egress_kind("http://user:pass@1.2.3.4:8000") == "api"


def test_plain_client_keeps_the_old_rule() -> None:
    c = EgressClient(None)
    assert c.egress_kind(None) == "direct"
    assert c.egress_kind("http://user:pass@1.2.3.4:8000") == "proxy"


def test_api_egress_id_cannot_collide_with_proxy6_ids() -> None:
    assert API_EGRESS_ID < 0


# --- лестница API ---------------------------------------------------------------


def test_api_ladder_is_a_single_rung() -> None:
    """Вторая ступень — это второй списанный кредит за тот же запрос."""
    for mp in ("ozon", "ym"):
        assert len(request_ladder(mp, via_api=True)) == 1
        assert request_ladder(mp, via_api=True) == API_LADDER[mp]


def test_api_ladder_replaces_rather_than_extends() -> None:
    assert request_ladder("ozon", via_api=True) != request_ladder("ozon")


@pytest.mark.parametrize("mp", ["ozon", "ym"])
def test_api_plan_keeps_the_budget_identity(mp: str) -> None:
    p = plan(15_000, mp, 0, via_api=True)
    assert p.total() == p.net_ms
    assert not p.guards, "одна ступень — межступенчатых гардов не бывает"


@pytest.mark.parametrize("mp", ["ozon", "ym"])
def test_api_lane_refuses_the_five_second_ceiling(mp: str) -> None:
    """Замер: Я.Маркет 4.6–6.3 с, Ozon 9.1–25.6 с. В 5 с не влезает ни один.

    Отказ планировщика здесь — правильный исход: обещать работу, которая
    заведомо не успеет, значит гарантировать таймаут вместо ответа.
    """
    with pytest.raises(BudgetTooSmall):
        plan(5_000, mp, 0, via_api=True)


def test_ozon_api_floor_matches_the_fastest_measurement() -> None:
    from mktlink.egress.scrapedo import OZON_FLOOR_MS

    assert API_LADDER["ozon"][0].floor_ms == OZON_FLOOR_MS


@pytest.mark.asyncio
async def test_provider_timeout_becomes_our_budget_problem() -> None:
    """Регрессия: таймаут уходил наверх и давал клиенту 500 capacity_exhausted.

    Это неправда дважды — ёмкость была, а виноват наш собственный потолок.
    ``DeadlineExceeded`` ловит ``run_ladder`` и делает ``BUDGET_EXHAUSTED``:
    вердикт, который не штрафует адрес и отдаётся как 504.
    """
    from curl_cffi.requests.exceptions import Timeout

    async def timing_out(url, *, headers, timeout_ms):
        raise Timeout("Operation timed out after 10126 milliseconds")

    t = ScrapeDoTransport(token="T", sender=timing_out)
    with pytest.raises(DeadlineExceeded):
        await t(
            "https://www.ozon.ru/product/x/",
            headers={}, proxy=None, impersonate=None, timeout_ms=1_000,
        )
