"""Корень композиции: где чистые модули впервые встречают внешний мир.

Собран отдельно от :mod:`mktlink.api.app` намеренно. Приложение принимает
готовые зависимости, поэтому весь путь запроса тестируется без сети, без
браузера и без proxy6 — а всё, что требует их наличия, живёт здесь и в
продакшене подменяется целиком.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from mktlink.api.routes import Deps, Extraction
from mktlink.budget import stage_reserve_ms
from mktlink.egress.client import EgressClient
from mktlink.egress.jars import JarStore
from mktlink.egress.spacing import reserve as reserve_slot
from mktlink.egress.spacing import wait as wait_slot
from mktlink.marketplaces.base import Context, run_ladder
from mktlink.marketplaces.lanes import SimpleLease, build_lane
from mktlink.marketplaces.selectors import REGISTRY as SELECTORS
from mktlink.marketplaces.verdict import Verdict
from mktlink.settings import Settings
from mktlink.store.cache import ProductCache
from mktlink.timing.deadline import Deadline, DeadlineExceeded
from mktlink.urls.canonical import Canonical

#: Маркетплейсы, где cookie — ПРЕДУСЛОВИЕ, а не обогащение.
#:
#: Только Ozon: его composer-api без cookie не отвечает вовсе (замер: 307
#: с рукопожатием, затем 403). У WB cookie не нужны, у Я.Маркета лёгкий jar
#: ничего не меняет — замерено, капча приходит одинаково с ним и без.
JAR_REQUIRED: frozenset[str] = frozenset({"ozon"})

#: Маркетплейсы, которые работают С НАШЕГО адреса напрямую, без прокси.
#:
#: ЗАМЕР 2026-09-03 и 2026-09-04: ``card.wb.ru/cards/v4/detail`` отвечает
#: ``200`` с датацентрового адреса в Финляндии, без cookie и без браузера,
#: и отдаёт название с продавцом за 0.6 с. Прокси ему не нужен.
#:
#: Зачем это отдельное множество, а не «пробуем всегда». Без него первый
#: запуск был сломан: свежая установка без купленного прокси отвечала на
#: ссылку WB ``202 no_warm_jar`` — то есть «нет тёплой сессии» на
#: маркетплейсе, которому сессия не нужна вовсе. Человек настраивал всё
#: правильно и получал молчание, пока не купит адрес, которым мы бы
#: всё равно не воспользовались.
#:
#: Прокси при этом остаётся ПРЕДПОЧТИТЕЛЬНЫМ: если он есть, идём через
#: него, потому что собственный адрес один и его репутацию мы не
#: контролируем. Прямой егресс — это фолбэк, а не замена.
WORKS_DIRECT: frozenset[str] = frozenset({"wb"})

#: Идентификатор «наш адрес без прокси» для таблицы ``spacing``.
#: Отрицательный, как и ``API_EGRESS_ID``, и отличный от него: спейсинг
#: считается по паре, и смешивать два разных егресса в одну пару значило
#: бы задерживать один из-за другого без причины.
DIRECT_EGRESS_ID: int = -2


@dataclass(slots=True)
class PoolView:
    """Что путь запроса знает о прокси.

    Намеренно узко: горячий путь умеет только СПРОСИТЬ адрес. Покупать,
    продлевать и списывать он не умеет вовсе — это работа forge, и граница
    здесь не соглашение, а отсутствие метода.
    """

    conn: sqlite3.Connection

    def lease(self) -> SimpleLease | None:
        row = self.conn.execute(
            "SELECT p6_id, host, port, user, pass FROM proxy"
            " WHERE state = 'active' AND never_renew = 0"
            " ORDER BY p6_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        url = f"http://{row['user']}:{row['pass']}@{row['host']}:{row['port']}"
        return SimpleLease(proxy_id=int(row["p6_id"]), proxy_url=url)


def build_ladder(
    conn: sqlite3.Connection,
    client: EgressClient,
    *,
    api_client: EgressClient | None = None,
    api_marketplaces: frozenset[str] = frozenset(),
):
    """Собрать боевую лестницу.

    Аренда адреса и слот спейсинга берутся один раз на запрос: все ступени
    идут через один адрес с одним jar, иначе вторая ступень предъявляла бы
    cookie, снятые не тем адресом.

    Транспорта два, а не один, и это следствие замера, а не гибкость впрок.
    Wildberries отвечает ``200`` с нашего собственного адреса за 7.70 ₽/мес;
    Ozon и Я.Маркет с него недостижимы вовсе и ходят через скрейпинг-API,
    где каждый запрос стоит кредитов. Один транспорт на всех означал бы либо
    платить за уже работающий WB, либо не получить два маркетплейса из трёх.
    ``api_marketplaces`` пустое — вся система работает как раньше.
    """
    jars = JarStore(conn)
    pool = PoolView(conn)

    def client_for(mp: str) -> EgressClient:
        if api_client is not None and mp in api_marketplaces:
            return api_client
        return client

    async def ladder(dl: Deadline, c: Canonical, budget_ms: int) -> Extraction:
        via_api = api_client is not None and c.marketplace in api_marketplaces

        if via_api:
            # Ни своего адреса, ни своих cookie здесь не нужно: и то и другое
            # предоставляет поставщик. Требовать аренду или jar на этом пути
            # значило бы отказывать в работе из-за отсутствия того, что в ней
            # не участвует, — и сообщать клиенту ложную причину «нет тёплой
            # сессии». Спейсинг при этом остаётся: см. API_EGRESS_ID.
            from mktlink.egress.scrapedo import API_EGRESS_ID  # noqa: PLC0415

            lease = SimpleLease(proxy_id=API_EGRESS_ID, proxy_url=None)
        else:
            got = pool.lease()
            if got is None:
                if c.marketplace in WORKS_DIRECT:
                    # Прокси нет, но этому маркетплейсу он и не нужен: замерено,
                    # что он отвечает с нашего адреса напрямую. Отказывать здесь
                    # значило бы требовать покупки ради адреса, которым мы не
                    # воспользуемся.
                    lease = SimpleLease(proxy_id=DIRECT_EGRESS_ID, proxy_url=None)
                else:
                    # Адреса нет вовсе. Это не ошибка запроса: forge купит
                    # первый, и повтор через Retry-After попадёт уже в рабочий
                    # пул.
                    return Extraction(
                        verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar"
                    )
            else:
                lease = got

            lease.jar = jars.get(c.marketplace, lease.proxy_id)
            if lease.jar is None and c.marketplace in JAR_REQUIRED:
                # Предусловие, а не обогащение: без cookie composer-api Ozon не
                # отвечает вовсе, и пробовать нечего.
                return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")
        # Для остальных jar — обогащение. Пробуем и без него: иначе вердикт
        # «нет тёплой сессии» подменял бы настоящую причину отказа.
        # ЗАМЕР 2026-09-03: карточка Я.Маркета отдаёт 302 на /showcaptcha
        # одинаково с cookie и без, поэтому требовать jar значило бы
        # сообщать клиенту не ту причину.

        slot = reserve_slot(conn, c.marketplace, lease.proxy_id)
        try:
            await wait_slot(dl, slot, reserve_ms=stage_reserve_ms(c.marketplace))
        except DeadlineExceeded:
            return Extraction(
                verdict=Verdict.BUDGET_EXHAUSTED, reason="spacing_wait_exceeds_budget"
            )

        lane_client = client_for(c.marketplace)
        lane = build_lane(c.marketplace, lane_client, SELECTORS, lease)
        ctx = Context(
            marketplace=c.marketplace,
            canonical_url=c.url,
            ids=dict(c.ids),
            offer=c.offer,
            anchor_ids=frozenset(c.ids.values()),
            via_api=via_api,
        )
        res, rung = await run_ladder(dl, lane, ctx, budget_ms, hops=0)

        _record(
            conn,
            c.marketplace,
            None if via_api else lease.proxy_id,
            res.verdict,
            egress=lane_client.egress_kind(lease.proxy_url),
        )

        return Extraction(
            verdict=res.verdict,
            name=res.name,
            seller_name=res.seller_name,
            seller_id=res.seller_id,
            seller_status=res.seller_status,
            seller_source=res.seller_source,
            legal_name=res.legal_name,
            rung=rung,
            confirmations=1 if res.verdict is Verdict.NOT_FOUND else 0,
        )

    return ladder


def _record(
    conn: sqlite3.Connection,
    mp: str,
    proxy_id: int | None,
    verdict: Verdict,
    *,
    egress: str = "proxy",
) -> None:
    """Записать исход. Штрафуется только то, что сделано ЧЕРЕЗ прокси.

    ``egress`` — параметр, а не константа, и это исправление дефекта.
    Прежняя редакция вписывала ``'proxy'`` литералом при любом транспорте.
    С появлением скрейпинг-API (:mod:`mktlink.egress.scrapedo`) это стало
    прямой порчей: капча, полученная с ЧУЖОГО адреса, записывалась как
    улика против нашего купленного IPv4 и приближала его замену за деньги.
    Предикат ``charges_proxy`` умел это различать всё время — ему просто
    никогда не передавали настоящий вид егресса.
    """
    from mktlink.marketplaces.verdict import charges_proxy  # noqa: PLC0415

    conn.execute(
        "INSERT INTO proxy_attempt (p6_id, mp, verdict, egress) VALUES (?, ?, ?, ?)",
        (proxy_id, mp, str(verdict), egress),
    )
    if egress != "proxy" or proxy_id is None:
        # Здоровье считается по паре (НАШ адрес, маркетплейс). Наблюдения с
        # чужого егресса в эту пару не входят вовсе, поэтому строки здоровья
        # для них не создаётся: иначе в таблице появился бы адрес, которым мы
        # не владеем и который нельзя ни продлить, ни заменить.
        return
    conn.execute(
        "INSERT INTO proxy_health (p6_id, mp, ok_n, bad_n) VALUES (?, ?, 0, 0)"
        " ON CONFLICT(p6_id, mp) DO NOTHING",
        (proxy_id, mp),
    )
    if charges_proxy(verdict, egress=egress):
        conn.execute(
            "UPDATE proxy_health SET bad_n = bad_n + 1, last_bad = unixepoch()"
            " WHERE p6_id = ? AND mp = ?",
            (proxy_id, mp),
        )
    elif egress == "proxy" and verdict in (Verdict.OK, Verdict.PARTIAL):
        conn.execute(
            "UPDATE proxy_health SET ok_n = ok_n + 1, last_ok = unixepoch()"
            " WHERE p6_id = ? AND mp = ?",
            (proxy_id, mp),
        )
    # SCHEMA_DRIFT и BUDGET_EXHAUSTED не трогают ни одну колонку: это наши
    # проблемы, а не свойства адреса.
    #
    # Условие ``egress == "proxy"`` в ветке успеха — зеркальная половина того
    # же исправления, и без неё оно было бы половинчатым. Успех, добытый
    # ЧУЖИМ адресом, начислял бы заслугу нашему: ``ok_n`` рос бы, доля отказов
    # падала, и адрес, который на самом деле ничего не отдаёт, выглядел бы
    # здоровым. Это опаснее лишнего штрафа — штраф ведёт к замене, а ложная
    # заслуга к тому, что мёртвый адрес держат вечно.
    # Строка ``proxy_attempt`` при этом пишется ВСЕГДА и со своим настоящим
    # егрессом: сырое наблюдение терять незачем, его отфильтрует читающий.


async def _resolve_dns(host: str) -> list[str]:
    """Резолв для SSRF-проверки.

    Отдельно от соединения намеренно: проверяем ВСЕ адреса ответа, а не тот,
    который выберет ОС. Ответ с одним публичным и одним приватным адресом
    иначе проходит и подключается к приватному.
    """
    import asyncio  # noqa: PLC0415
    import socket  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


async def _fetch_hop(url: str, timeout_ms: int) -> tuple[int, str | None]:
    """Один хоп раскрутки. Прямой егресс, без прокси, без автоследования."""
    from curl_cffi.requests import AsyncSession  # noqa: PLC0415
    from curl_cffi.requests.exceptions import Timeout  # noqa: PLC0415

    from mktlink.urls.redirects import HOP_HEADERS  # noqa: PLC0415

    try:
        async with AsyncSession(trust_env=False) as s:
            r = await s.get(
                url,
                headers=HOP_HEADERS,
                timeout=timeout_ms / 1000,
                allow_redirects=False,
            )
            return r.status_code, r.headers.get("Location")
    except Timeout:
        raise DeadlineExceeded("unwind_transport") from None


def build_deps(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> Deps:
    cfg = settings or Settings()
    from mktlink.store.db import connect  # noqa: PLC0415
    from mktlink.urls.redirects import RedirectResolver  # noqa: PLC0415

    c = conn or connect(cfg.db_path)
    client = EgressClient()
    # Redis необязателен: без URL всё работает на SQLite, как раньше.
    from mktlink.store.rediscache import open_layer  # noqa: PLC0415
    from mktlink.store.unwound import UnwoundLinks  # noqa: PLC0415

    redis_layer = open_layer(cfg.redis_url)
    # Второй клиент поднимается ТОЛЬКО когда ключ задан. Без ключа система
    # ведёт себя ровно как до появления скрейпинг-API, и это важно: путь
    # через чужой сервис не должен включаться сам собой.
    api_client: EgressClient | None = None
    api_mps: frozenset[str] = frozenset()
    if cfg.scrapedo_configured:
        from mktlink.egress.scrapedo import ScrapeDoTransport  # noqa: PLC0415
        from mktlink.store.shortlinks import OutboundShortlinks  # noqa: PLC0415

        api_client = EgressClient(
            ScrapeDoTransport(
                token=cfg.scrapedo_token or "",
                shorten_via=cfg.scrapedo_shorten_via,
                shorten_cache=OutboundShortlinks(c),
            )
        )
        api_mps = frozenset(cfg.scrapedo_marketplaces)
    return Deps(
        cache=ProductCache(c, redis=redis_layer),
        ladder=build_ladder(c, client, api_client=api_client, api_marketplaces=api_mps),
        # Тот же набор уходит и в Deps: предпроверка бюджета в routes.py
        # обязана проверять ту лестницу, по которой запрос реально пойдёт.
        api_marketplaces=api_mps,
        unwound=UnwoundLinks(c),
        product_ttl_s=cfg.product_ttl_s,
        # 0 означает «как модельный»: ручка есть, но выключаема одним нулём,
        # а не требует держать два согласованных числа.
        product_ttl_pinned_s=cfg.product_ttl_pinned_s or cfg.product_ttl_s,
        # Требование 2 подключено здесь и только здесь. Раскрутка идёт прямым
        # егрессом: каждый её хоп через прокси взял бы слот спейсинга, и при
        # интервале Ozon в 5 с лестницы для короткой ссылки не осталось бы.
        resolver=RedirectResolver(_fetch_hop, _resolve_dns),
        budget_ms=cfg.response_budget_ms,
        ym_region_id=cfg.ym_region_id,
    )
