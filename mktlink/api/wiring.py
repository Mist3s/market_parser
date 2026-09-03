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


def build_ladder(conn: sqlite3.Connection, client: EgressClient):
    """Собрать боевую лестницу.

    Аренда адреса и слот спейсинга берутся один раз на запрос: все ступени
    идут через один адрес с одним jar, иначе вторая ступень предъявляла бы
    cookie, снятые не тем адресом.
    """
    jars = JarStore(conn)
    pool = PoolView(conn)

    async def ladder(dl: Deadline, c: Canonical, budget_ms: int) -> Extraction:
        lease = pool.lease()
        if lease is None:
            # Адреса нет вовсе. Это не ошибка запроса: forge купит первый,
            # и повтор через Retry-After попадёт уже в рабочий пул.
            return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")

        lease.jar = jars.get(c.marketplace, lease.proxy_id)
        if lease.jar is None and c.marketplace != "wb":
            # WB не требует cookie; Ozon и Я.Маркет — требуют.
            return Extraction(verdict=Verdict.SILENT_EMPTY, reason="no_warm_jar")

        slot = reserve_slot(conn, c.marketplace, lease.proxy_id)
        try:
            await wait_slot(dl, slot, reserve_ms=stage_reserve_ms(c.marketplace))
        except DeadlineExceeded:
            return Extraction(
                verdict=Verdict.BUDGET_EXHAUSTED, reason="spacing_wait_exceeds_budget"
            )

        lane = build_lane(c.marketplace, client, SELECTORS, lease)
        ctx = Context(
            marketplace=c.marketplace,
            canonical_url=c.url,
            ids=dict(c.ids),
            offer=c.offer,
            anchor_ids=frozenset(c.ids.values()),
        )
        res, rung = await run_ladder(dl, lane, ctx, budget_ms, hops=0)

        _record(conn, c.marketplace, lease.proxy_id, res.verdict)

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


def _record(conn: sqlite3.Connection, mp: str, proxy_id: int, verdict: Verdict) -> None:
    """Записать исход. Штрафуется только то, что сделано ЧЕРЕЗ прокси."""
    from mktlink.marketplaces.verdict import charges_proxy  # noqa: PLC0415

    conn.execute(
        "INSERT INTO proxy_attempt (p6_id, mp, verdict, egress) VALUES (?, ?, ?, 'proxy')",
        (proxy_id, mp, str(verdict)),
    )
    conn.execute(
        "INSERT INTO proxy_health (p6_id, mp, ok_n, bad_n) VALUES (?, ?, 0, 0)"
        " ON CONFLICT(p6_id, mp) DO NOTHING",
        (proxy_id, mp),
    )
    if charges_proxy(verdict, egress="proxy"):
        conn.execute(
            "UPDATE proxy_health SET bad_n = bad_n + 1, last_bad = unixepoch()"
            " WHERE p6_id = ? AND mp = ?",
            (proxy_id, mp),
        )
    elif verdict in (Verdict.OK, Verdict.PARTIAL):
        conn.execute(
            "UPDATE proxy_health SET ok_n = ok_n + 1, last_ok = unixepoch()"
            " WHERE p6_id = ? AND mp = ?",
            (proxy_id, mp),
        )
    # SCHEMA_DRIFT и BUDGET_EXHAUSTED не трогают ни одну колонку: это наши
    # проблемы, а не свойства адреса.


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

    from mktlink.urls.redirects import HOP_HEADERS  # noqa: PLC0415

    async with AsyncSession(trust_env=False) as s:
        r = await s.get(
            url,
            headers=HOP_HEADERS,
            timeout=timeout_ms / 1000,
            allow_redirects=False,
        )
        return r.status_code, r.headers.get("Location")


def build_deps(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> Deps:
    cfg = settings or Settings()
    from mktlink.store.db import connect  # noqa: PLC0415
    from mktlink.urls.redirects import RedirectResolver  # noqa: PLC0415

    c = conn or connect(cfg.db_path)
    client = EgressClient()
    return Deps(
        cache=ProductCache(c),
        ladder=build_ladder(c, client),
        # Требование 2 подключено здесь и только здесь. Раскрутка идёт прямым
        # егрессом: каждый её хоп через прокси взял бы слот спейсинга, и при
        # интервале Ozon в 5 с лестницы для короткой ссылки не осталось бы.
        resolver=RedirectResolver(_fetch_hop, _resolve_dns),
        budget_ms=cfg.response_budget_ms,
        ym_region_id=cfg.ym_region_id,
    )
