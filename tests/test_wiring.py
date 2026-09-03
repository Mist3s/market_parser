"""Сквозной путь: реестр -> лестница -> лейн -> сеть, всё кроме сокета."""

from __future__ import annotations

import json
import time

import pytest

from mktlink.api.routes import Deps, handle
from mktlink.api.schemas import ProductRequest
from mktlink.api.wiring import PoolView, build_ladder
from mktlink.egress.client import EgressClient
from mktlink.egress.jars import Jar, JarStore
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db

WB_PDP = "https://www.wildberries.ru/catalog/12345678/detail.aspx"
YM_PDP = "https://market.yandex.ru/card/pyure/4382957723"


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "w.sqlite")
    c = connect(tmp_path / "w.sqlite")
    c.execute(
        "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state, term_end)"
        " VALUES (7, '1.2.3.4', 'h', 8000, 'u', 'p', 3, 'mp1.mp.s.a.R.ru.g01', 'active',"
        " unixepoch() + 864000)"
    )
    yield c
    c.close()


def transport(body: str, status: int = 200, sink=None):
    async def send(url, *, headers, proxy, impersonate, timeout_ms):
        if sink is not None:
            sink.append({"url": url, "proxy": proxy, "headers": headers})
        return status, body

    return send


WB_OK = json.dumps(
    {"data": {"products": [
        {"id": 12345678, "name": "Смесь Nutrilon", "supplier": "ООО «Ромашка»",
         "supplierId": 999}
    ]}},
    ensure_ascii=False,
)


async def test_end_to_end_wb_needs_no_jar(conn) -> None:
    """WB — единственный лейн, где cookie не нужны."""
    sink: list[dict] = []
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport(WB_OK, sink=sink))))
    code, r = await handle(ProductRequest(url=WB_PDP), deps)
    assert code == 200 and r.status == "ok"
    assert r.product.name == "Смесь Nutrilon"
    assert r.seller.name == "ООО «Ромашка»"
    assert sink[0]["proxy"] == "http://u:p@h:8000", "запрос ушёл через прокси"
    assert "dest=-1257786" in sink[0]["url"], "регион запинен"


async def test_ozon_without_a_jar_answers_pending_not_a_lie(conn) -> None:
    """composer-api без cookie не отвечает, и притворяться нечем."""
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport("{}"))))
    code, r = await handle(
        ProductRequest(url="https://www.ozon.ru/product/smes-1234567890/"), deps
    )
    assert code == 202 and r.status == "pending"
    assert r.meta.reason == "no_warm_jar"
    assert r.url.canonical is not None, "канонический URL всё равно отдан"


async def test_ym_with_a_warm_jar_goes_through(conn) -> None:
    jars = JarStore(conn)
    jars.put_unverified(Jar("ym", 7, "yandexuid=1", "Mozilla/5.0 Firefox/133.0", 133,
                            int(time.time())))
    jars.publish("ym", 7)

    state = json.dumps(
        {"widgets": {"DefaultOffer": {"x": {"skuId": "4382957723",
                                            "shop": {"name": "ООО «Ромашка»", "id": 5}}}}},
        ensure_ascii=False,
    )
    html = (
        '<html><head><meta property="og:url" content="https://market.yandex.ru/card/x/4382957723">'
        f"</head><body><h1>Пюре Semper</h1><script>{state}</script>{'x' * 25000}</body></html>"
    )
    sink: list[dict] = []
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport(html, sink=sink))))
    code, r = await handle(ProductRequest(url=YM_PDP), deps)
    assert code == 200
    assert r.product.name == "Пюре Semper"
    assert r.seller.name == "ООО «Ромашка»"
    assert "lr=213" in sink[0]["url"], "регион форсирован"
    assert sink[0]["headers"]["Cookie"] == "yandexuid=1"


async def test_an_empty_pool_is_pending_not_an_error(conn) -> None:
    """Пустой пул — это «подожди», а не ошибка. Но только там, где адрес НУЖЕН.

    Проверяется на Я.Маркете, а не на WB, и это не придирка. WB замерен
    работающим с нашего адреса напрямую (HTTP 200 без cookie и без браузера),
    поэтому требовать для него покупку значило бы ломать первый запуск: свежая
    установка отвечала бы `202` на маркетплейсе, которому прокси не нужен —
    см. ``WORKS_DIRECT``. У Ozon и Я.Маркета адрес действительно необходим:
    с нашего они замерены как 403 и как капча.
    """
    conn.execute("DELETE FROM proxy")
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport(WB_OK))))
    code, r = await handle(ProductRequest(url=YM_PDP), deps)
    assert code == 202, "forge купит первый адрес, повтор попадёт в рабочий пул"
    assert r.meta.reason == "no_warm_jar"

    # А WB на том же пустом пуле обязан ответить, а не ждать покупки.
    code, r = await handle(ProductRequest(url=WB_PDP), deps)
    assert code == 200, r.meta.reason


async def test_a_silent_block_is_charged_to_the_proxy(conn) -> None:
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport('{"data": {"products": []}}'))))
    await handle(ProductRequest(url=WB_PDP), deps)
    row = conn.execute(
        "SELECT bad_n FROM proxy_health WHERE p6_id = 7 AND mp = 'wb'"
    ).fetchone()
    assert row["bad_n"] == 1


async def test_schema_drift_is_not_charged_to_the_proxy(conn) -> None:
    """Маркетплейс переименовал поле — виноват наш парсер, а не адрес."""
    drifted = json.dumps({"data": {"products": [{"id": 12345678}]}})
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport(drifted))))
    await handle(ProductRequest(url=WB_PDP), deps)
    row = conn.execute(
        "SELECT ok_n, bad_n FROM proxy_health WHERE p6_id = 7 AND mp = 'wb'"
    ).fetchone()
    assert (row["ok_n"], row["bad_n"]) == (0, 0), "ни одна колонка не тронута"


async def test_every_attempt_is_recorded_with_its_egress(conn) -> None:
    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(
        transport(WB_OK))))
    await handle(ProductRequest(url=WB_PDP), deps)
    row = conn.execute("SELECT egress, verdict FROM proxy_attempt").fetchone()
    assert row["egress"] == "proxy"


def test_the_hot_path_cannot_buy_or_prolong(conn) -> None:
    """Граница не соглашение, а отсутствие метода."""
    view = PoolView(conn)
    assert hasattr(view, "lease")
    for forbidden in ("buy", "prolong", "condemn", "delete"):
        assert not hasattr(view, forbidden), forbidden


async def test_only_ozon_treats_a_jar_as_a_precondition(conn) -> None:
    """Требовать jar там, где он ничего не меняет, значит врать о причине.

    ЗАМЕР: карточка Я.Маркета отдаёт 302 на /showcaptcha одинаково с cookie и
    без, поэтому ответ «нет тёплой сессии» подменял бы настоящую причину —
    капчу. У Ozon же composer-api без cookie не отвечает вовсе, и там это
    настоящее предусловие.
    """
    from mktlink.api.wiring import JAR_REQUIRED

    assert JAR_REQUIRED == frozenset({"ozon"})

    tried: list[str] = []

    async def watching(url, *, headers, proxy, impersonate, timeout_ms):
        tried.append(url)
        return 302, ""

    deps = Deps(cache=ProductCache(conn), ladder=build_ladder(conn, EgressClient(watching)))
    code, r = await handle(ProductRequest(url=YM_PDP), deps)
    assert tried, "Я.Маркет обязан быть попробован без jar"
    # Главное утверждение теста — вот это: причина не может быть «нет тёплой
    # сессии», потому что попытка БЫЛА сделана.
    assert r.meta.reason != "no_warm_jar", "иначе клиенту сообщается не та причина"
    # А конкретная причина уточнена по замеру: 302 у карточки Я.Маркета — это
    # увод на /showcaptcha, проверено дважды (2026-09-03 и 2026-09-04). Раньше
    # здесь стояло «marketplace_silent» — «страница пришла пустая», — потому
    # что тело у 302 действительно пустое, и классификатор доходил до проверки
    # идентичности. Диагноз был неверный: нас челленджили, а не отдавали пусто.
    assert r.meta.reason == "marketplace_challenge", r.meta.reason
