"""Атрибуция егресса: чужой адрес не влияет на здоровье нашего.

Это тесты про деньги, а не про формат. Здоровье пары ``(наш адрес,
маркетплейс)`` — единственное основание для замены адреса, а замена стоит
закупки. Значит любая примесь в этой статистике либо тратит деньги зря, либо
держит мёртвый адрес в работе.
"""

from __future__ import annotations

import pathlib

import pytest

from mktlink.api.routes import Deps, handle
from mktlink.api.schemas import ProductRequest
from mktlink.api.wiring import build_ladder
from mktlink.egress.client import EgressClient
from mktlink.egress.scrapedo import API_EGRESS_ID, ScrapeDoTransport
from mktlink.marketplaces.verdict import Verdict, charges_proxy
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
OZON_PDP = (
    "https://www.ozon.ru/product/"
    "kitayskiy-chay-shu-puer-tszin-ya-syao-pin-zolotye-pochki-syre-2009g-100gr-3160461596/"
)


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "a.sqlite")
    c = connect(tmp_path / "a.sqlite")
    c.execute(
        "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state, term_end)"
        " VALUES (7, '1.2.3.4', 'h', 8000, 'u', 'p', 3, 'mp1.mp.s.a.R.ru.g01', 'active',"
        " unixepoch() + 864000)"
    )
    yield c
    c.close()


def _api_client(status: int, body: str):
    async def send(url, *, headers, timeout_ms):
        return status, body

    return EgressClient(ScrapeDoTransport(token="T", sender=send))


async def _run(conn, client, url: str = OZON_PDP):
    deps = Deps(
        cache=ProductCache(conn),
        ladder=build_ladder(
            conn,
            EgressClient(None),
            api_client=client,
            api_marketplaces=frozenset({"ozon", "ym"}),
        ),
    )
    return await handle(ProductRequest(url=url), deps, request_id="r1")


# --- предикат ------------------------------------------------------------------


def test_predicate_never_charges_a_foreign_egress() -> None:
    for verdict in Verdict:
        assert not charges_proxy(verdict, egress="api"), verdict
    # А через наш прокси — по-прежнему штрафует то, что должно.
    assert charges_proxy(Verdict.CAPTCHA, egress="proxy")


# --- запись ---------------------------------------------------------------------


async def test_api_success_does_not_credit_our_proxy(conn) -> None:
    """Успех чужим адресом не начисляет заслугу нашему.

    Это опаснее лишнего штрафа: штраф ведёт к замене, а ложная заслуга — к
    тому, что мёртвый адрес держат в пуле вечно.
    """
    html = (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")
    code, body = await _run(conn, _api_client(200, html))
    assert code == 200
    assert body.seller.name == "Шуняня"

    rows = conn.execute("SELECT ok_n, bad_n FROM proxy_health").fetchall()
    assert rows == [], "строки здоровья для чужого егресса быть не должно"


async def test_api_captcha_does_not_charge_our_proxy(conn) -> None:
    block = "<html><title>Похоже, нет соединения</title></html>" + "x" * 6000
    await _run(conn, _api_client(200, block))
    assert conn.execute("SELECT count(*) c FROM proxy_health").fetchone()["c"] == 0


async def test_the_raw_observation_is_still_recorded(conn) -> None:
    """Сырое наблюдение не теряется — теряется только его влияние на здоровье."""
    html = (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")
    await _run(conn, _api_client(200, html))
    row = conn.execute("SELECT p6_id, mp, egress FROM proxy_attempt").fetchone()
    assert row["egress"] == "api"
    assert row["p6_id"] is None, "чужой адрес нельзя записать нашим идентификатором"
    assert row["mp"] == "ozon"


async def test_our_own_egress_still_updates_health(conn) -> None:
    """Контроль: исправление не должно было отключить учёт вообще."""
    from mktlink.api.wiring import _record

    _record(conn, "ozon", 7, Verdict.CAPTCHA, egress="proxy")
    row = conn.execute("SELECT ok_n, bad_n FROM proxy_health WHERE p6_id = 7").fetchone()
    assert (row["ok_n"], row["bad_n"]) == (0, 1)

    _record(conn, "ozon", 7, Verdict.OK, egress="proxy")
    row = conn.execute("SELECT ok_n, bad_n FROM proxy_health WHERE p6_id = 7").fetchone()
    assert (row["ok_n"], row["bad_n"]) == (1, 1)


# --- отсутствие предусловий на API-пути -----------------------------------------


async def test_api_path_needs_neither_lease_nor_jar(tmp_path) -> None:
    """Ни адреса, ни cookie: и то и другое даёт поставщик.

    Пул в этой БД ПУСТ намеренно. До исправления такой запрос отвечал
    ``202`` с причиной «нет тёплой сессии» — то есть сообщал клиенту
    отсутствие того, что в его обработке не участвует.
    """
    init_db(tmp_path / "empty.sqlite")
    c = connect(tmp_path / "empty.sqlite")
    try:
        html = (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")
        code, body = await _run(c, _api_client(200, html))
        assert code == 200
        assert body.product.name is not None
        assert body.seller.name == "Шуняня"
    finally:
        c.close()


async def test_spacing_is_still_applied_on_the_api_path(conn) -> None:
    """Слот спейсинга берётся и здесь: частота обращений — наша ответственность.

    Заодно это предохранитель от петли, которая молча съест месячную квоту
    кредитов.
    """
    html = (FIXTURES / "ozon_pdp.html").read_text(encoding="utf-8")
    await _run(conn, _api_client(200, html))
    row = conn.execute("SELECT proxy_id FROM spacing WHERE mp = 'ozon'").fetchone()
    assert row is not None
    assert row["proxy_id"] == API_EGRESS_ID
    assert row["proxy_id"] < 0, "не может совпасть ни с одним идентификатором proxy6"


# --- первый запуск без покупок ---------------------------------------------------


async def test_wb_works_on_a_fresh_install_with_no_proxy(tmp_path) -> None:
    """Регрессия первого запуска, а не оптимизация.

    До исправления свежая установка без купленного прокси отвечала на ссылку
    WB ``202 no_warm_jar`` — «нет тёплой сессии» на маркетплейсе, которому
    сессия не нужна вовсе (замерено: HTTP 200 без cookie и без браузера).
    Человек настраивал всё правильно и получал молчание, пока не купит адрес,
    которым мы бы не воспользовались.
    """
    from mktlink.api.wiring import DIRECT_EGRESS_ID, WORKS_DIRECT, build_ladder

    assert "wb" in WORKS_DIRECT
    assert "ozon" not in WORKS_DIRECT, "Ozon с нашего адреса замерен как 403"
    assert "ym" not in WORKS_DIRECT, "Я.Маркет с нашего адреса замерен как капча"

    init_db(tmp_path / "fresh.sqlite")
    c = connect(tmp_path / "fresh.sqlite")
    try:
        wb_ok = (
            '{"products":[{"id":430803464,"name":"Да Хун Пао","supplier":"BASKEY",'
            '"supplierId":55588}]}'
        )

        async def send(url, *, headers, proxy, impersonate, timeout_ms):
            assert proxy is None, "прокси нет — идём напрямую"
            return 200, wb_ok

        deps = Deps(
            cache=ProductCache(c),
            ladder=build_ladder(c, EgressClient(send)),
        )
        code, body = await handle(
            ProductRequest(url="https://www.wildberries.ru/catalog/430803464/detail.aspx"),
            deps,
        )
        assert code == 200, body.meta.reason
        assert body.product.name == "Да Хун Пао"
        assert body.seller.name == "BASKEY"

        # Наблюдение записано как 'direct' и здоровья ничьего не тронуло.
        row = c.execute("SELECT p6_id, egress FROM proxy_attempt").fetchone()
        assert row["egress"] == "direct"
        assert c.execute("SELECT count(*) n FROM proxy_health").fetchone()["n"] == 0
        # Спейсинг всё равно взят, под своим идентификатором.
        slot = c.execute("SELECT proxy_id FROM spacing WHERE mp = 'wb'").fetchone()
        assert slot["proxy_id"] == DIRECT_EGRESS_ID
    finally:
        c.close()


async def test_a_real_proxy_is_still_preferred(conn) -> None:
    """Прямой егресс — фолбэк, а не замена: свой адрес один и без репутации."""
    from mktlink.api.wiring import DIRECT_EGRESS_ID, build_ladder

    seen: list[str | None] = []
    wb_ok = '{"products":[{"id":1,"name":"Ч","supplier":"S","supplierId":2}]}'

    async def send(url, *, headers, proxy, impersonate, timeout_ms):
        seen.append(proxy)
        return 200, wb_ok

    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(send)))
    await handle(
        ProductRequest(url="https://www.wildberries.ru/catalog/430803464/detail.aspx"), deps
    )
    assert seen and seen[0] is not None, "в фикстуре прокси есть — он и должен идти в дело"
    row = conn.execute("SELECT proxy_id FROM spacing WHERE mp = 'wb'").fetchone()
    assert row["proxy_id"] != DIRECT_EGRESS_ID
