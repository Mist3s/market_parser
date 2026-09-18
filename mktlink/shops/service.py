"""Путь запроса ``/v1/shop``: ссылка на карточку → название чая и магазина.

Порядок стадий тот же, что у маркетплейсов, и по той же причине: сперва всё,
что не открывает сокет (валидация, реестр, канонический URL, кэш), потом один
сетевой запрос, потом разбор. Дешёвые отказы стоят миллисекунды.

Чего здесь НЕТ по сравнению с :mod:`mktlink.api.routes`, и почему:

* лестницы ступеней — источник один, страница;
* продавца — магазин сам себе продавец, его имя берётся из реестра;
* спейсинга и jar — магазины отвечают напрямую и без cookie (замер в
  :mod:`mktlink.shops.fetch`);
* цен — контракт этого эндпоинта: название и магазин, ничего больше.

Кэш — тот же :class:`mktlink.store.cache.ProductCache` с ключом ``shop:…``.
Название товара не меняется, поэтому срок свежести по умолчанию — неделя, то
есть горизонт устаревания: запись живёт столько, сколько может быть отдана.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from mktlink.api.errors import http_for, retry_after_for
from mktlink.api.routes import Cache
from mktlink.api.schemas import (
    ShopBlock,
    ShopMetaBlock,
    ShopProductBlock,
    ShopRequest,
    ShopResponse,
    UrlBlock,
)
from mktlink.constants import (
    SHOP_BUDGET_DEFAULT_MS,
    SHOP_BUDGET_FLOOR_MS,
    SHOP_RESERVE_MS,
    SHOP_TAIL_MS,
    SHOP_TTL_S,
)
from mktlink.egress.client import BodyTooLarge
from mktlink.shops.extract import extract_name, looks_like_not_found, site_name
from mktlink.shops.fetch import Fetched, ShopFetcher, ShopUnreachable
from mktlink.shops.registry import Shop, lookup
from mktlink.timing.deadline import Deadline, DeadlineExceeded, bind, unbind
from mktlink.urls.ssrf import SsrfRejected, check_resolved
from mktlink.urls.validate import UrlRejected, validate


@dataclass(slots=True)
class ShopDeps:
    cache: Cache
    fetcher: ShopFetcher
    lookup: Callable[[str], Shop | None] = lookup
    budget_ms: int = SHOP_BUDGET_DEFAULT_MS
    ttl_s: int = SHOP_TTL_S
    #: Незнакомые хосты — только по явной настройке: без allowlist'а сервис
    #: превращается в открытый прокси для чужих запросов.
    allow_unknown_hosts: bool = False
    #: Резолв для SSRF-проверки незнакомого хоста. У хостов из реестра не
    #: нужен: они проверены человеком при добавлении.
    resolve_dns: Callable[[str], Awaitable[list[str]]] | None = None
    #: Место для метрик и т.п., чтобы не менять сигнатуру.
    extra: dict[str, Any] = field(default_factory=dict)


async def handle_shop(
    req: ShopRequest, deps: ShopDeps, *, request_id: str | None = None
) -> tuple[int, ShopResponse]:
    rid = request_id or uuid.uuid4().hex
    budget = min(deps.budget_ms, req.max_wait_ms or deps.budget_ms)
    if budget < SHOP_BUDGET_FLOOR_MS:
        return _reject("invalid_budget", rid, req.url, detail={"floor_ms": SHOP_BUDGET_FLOOR_MS})

    dl = Deadline.start(budget - SHOP_TAIL_MS, rid)
    token = bind(dl)
    try:
        return await _run(req, deps, dl, rid, budget)
    finally:
        unbind(token)


async def _run(
    req: ShopRequest, deps: ShopDeps, dl: Deadline, rid: str, budget: int
) -> tuple[int, ShopResponse]:
    # --- разбор и реестр: без сети -------------------------------------------
    try:
        parsed = validate(req.url)
    except UrlRejected as exc:
        return _reject(exc.code, rid, req.url, detail={"reason": exc.detail})

    shop = deps.lookup(parsed.host)
    if shop is None:
        if not deps.allow_unknown_hosts:
            return _reject("shop_not_supported", rid, req.url, detail={"host": parsed.host})
        if deps.resolve_dns is not None:
            try:
                check_resolved(await deps.resolve_dns(parsed.host))
            except SsrfRejected as exc:
                return _reject("host_not_allowed", rid, req.url, detail={"reason": exc.reason})
            except OSError as exc:
                return _reject(
                    "host_not_allowed", rid, req.url,
                    detail={"reason": "dns_failed", "error": str(exc)},
                )
        shop = Shop.generic(parsed.host)

    if not shop.is_product_path(parsed.path):
        return _reject(
            "not_a_product_url", rid, req.url, shop=shop, detail={"path": parsed.path}
        )

    canonical = shop.canonical(parsed.path, parsed.query)
    key = f"shop:v1:{shop.host}:{canonical.removeprefix(f'https://{shop.host}').rstrip('/')}"

    # --- кэш ----------------------------------------------------------------
    hit = None if req.force_refresh else deps.cache.get(key, fresh_ttl_s=deps.ttl_s)
    if hit is not None:
        age = max(0, int(time.time() - float(hit.get("fetched_at") or 0)))
        return 200, _ok(hit, rid, req.url, canonical, shop, budget, dl, cache="hit", age_s=age)

    # --- один сетевой запрос ---------------------------------------------------
    try:
        got = await deps.fetcher.fetch(
            dl, shop.fetch_url(canonical), cap_ms=dl.remaining_ms, reserve_ms=SHOP_RESERVE_MS
        )
    except DeadlineExceeded:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         status="deadline_exceeded", reason="deadline_exhausted")
    except ShopUnreachable as exc:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         reason="shop_unreachable", detail={"error": exc.reason})
    except BodyTooLarge as exc:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         reason="body_too_large", detail={"error": str(exc)})

    # Снятый товар часто уводит редиректом на главную или в категорию. Это
    # не карточка, и сказать это надо прямо, а не отдавать название категории.
    final_host = (urlsplit(got.final_url).hostname or "").casefold()
    final_path = urlsplit(got.final_url).path or "/"
    if final_host and final_host not in shop.hosts:
        return _reject("not_a_product_url", rid, req.url, shop=shop, canonical=canonical,
                       detail={"redirected_to": got.final_url})
    if final_host and not shop.is_product_path(final_path):
        return 404, _envelope("not_found", rid, req.url, canonical, shop, budget, dl,
                              egress=got.egress, detail={"redirected_to": got.final_url})

    if got.status in (404, 410):
        return 404, _envelope("not_found", rid, req.url, canonical, shop, budget, dl,
                              egress=got.egress)
    if got.blocked:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         reason="shop_blocked", egress=got.egress,
                         detail={"http_status": got.status})
    if got.status >= 500 or got.status >= 300:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         reason="shop_error", egress=got.egress,
                         detail={"http_status": got.status})

    # --- разбор -----------------------------------------------------------------
    if looks_like_not_found(got.body):
        return 404, _envelope("not_found", rid, req.url, canonical, shop, budget, dl,
                              egress=got.egress, detail={"soft": True})
    if shop.product_marker is not None and shop.product_marker.search(got.body) is None:
        return _reject("not_a_product_url", rid, req.url, shop=shop, canonical=canonical,
                       detail={"reason": "no_product_marker"})

    found = extract_name(got.body, shop.name_sources)
    if found.name is None:
        return _fallback(req, deps, key, rid, canonical, shop, budget, dl,
                         reason="layout_changed", egress=got.egress,
                         detail={"candidates": [list(c) for c in found.candidates]})

    body = {
        "name": found.name,
        "source": found.source,
        "shop_name": shop.name or site_name(got.body) or shop.host,
        "platform": shop.platform,
        "canonical": canonical,
        "egress": got.egress,
        # Уровень памяти кэша возраст не хранит, поэтому он лежит в самом теле.
        "fetched_at": time.time(),
    }
    deps.cache.put(key, body)
    return 200, _ok(body, rid, req.url, canonical, shop, budget, dl, cache="miss", got=got)


# --- сборка ответов ---------------------------------------------------------------


def _shop_block(shop: Shop | None, name: str | None = None) -> ShopBlock:
    if shop is None:
        return ShopBlock()
    return ShopBlock(host=shop.host, name=name or shop.name, platform=shop.platform)


def _reject(
    status: str,
    rid: str,
    submitted: str,
    *,
    shop: Shop | None = None,
    canonical: str | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[int, ShopResponse]:
    return http_for(status), ShopResponse(
        status=status,  # type: ignore[arg-type]
        request_id=rid,
        url=UrlBlock(submitted=submitted, canonical=canonical),
        shop=_shop_block(shop),
        meta=ShopMetaBlock(detail=detail or {}),
    )


def _envelope(
    status: str,
    rid: str,
    submitted: str,
    canonical: str,
    shop: Shop,
    budget: int,
    dl: Deadline,
    *,
    egress: str | None = None,
    detail: dict[str, Any] | None = None,
    **meta: Any,
) -> ShopResponse:
    return ShopResponse(
        status=status,  # type: ignore[arg-type]
        request_id=rid,
        url=UrlBlock(submitted=submitted, canonical=canonical),
        shop=_shop_block(shop),
        meta=ShopMetaBlock(
            budget_ms=budget,
            elapsed_ms=dl.elapsed_ms,
            egress=egress,
            ledger=list(dl.spent),
            detail=detail or {},
            **meta,
        ),
    )


def _fallback(
    req: ShopRequest,
    deps: ShopDeps,
    key: str,
    rid: str,
    canonical: str,
    shop: Shop,
    budget: int,
    dl: Deadline,
    *,
    reason: str,
    status: str = "pending",
    egress: str | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[int, ShopResponse]:
    """Не смогли сейчас: сначала устаревшее из кэша, если разрешено, иначе отказ.

    Отдать старое название лучше, чем ничего: оно не меняется. Но только с
    возрастом и статусом ``stale`` — тихой подмены здесь нет.
    """
    if req.max_stale_s > 0:
        stale = deps.cache.get_stale(key, req.max_stale_s)
        if stale is not None:
            body, age = stale
            resp = _ok(body, rid, req.url, canonical, shop, budget, dl, cache="stale", age_s=age)
            resp.status = "stale"
            resp.meta.degraded = True
            resp.meta.reason = reason
            return 200, resp
    resp = _envelope(status, rid, req.url, canonical, shop, budget, dl,
                     egress=egress, detail=detail, reason=reason)
    if status == "pending":
        resp.meta.retry_after_seconds = retry_after_for(reason)
    return http_for(status) if status != "pending" else 202, resp


def _ok(
    body: dict[str, Any],
    rid: str,
    submitted: str,
    canonical: str,
    shop: Shop,
    budget: int,
    dl: Deadline,
    *,
    cache: str,
    got: Fetched | None = None,
    age_s: int | None = None,
) -> ShopResponse:
    return ShopResponse(
        status="ok",
        request_id=rid,
        url=UrlBlock(submitted=submitted, canonical=body.get("canonical") or canonical),
        shop=_shop_block(shop, body.get("shop_name")),
        product=ShopProductBlock(name=body.get("name"), source=body.get("source")),
        meta=ShopMetaBlock(
            budget_ms=budget,
            elapsed_ms=dl.elapsed_ms,
            cache=cache,  # type: ignore[arg-type]
            egress=got.egress if got is not None else body.get("egress"),
            ledger=list(dl.spent),
            detail={"age_s": age_s} if age_s is not None else {},
        ),
    )


__all__ = ["ShopDeps", "handle_shop"]
