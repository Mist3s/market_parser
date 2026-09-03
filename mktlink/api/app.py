"""Фабрика FastAPI.

Приложение тонкое намеренно: вся логика в :mod:`mktlink.api.routes`, который
не знает про HTTP и потому тестируется без клиента и без сети. Здесь остаётся
только то, что действительно про транспорт.

Балкхед ``GLOBAL_SEMAPHORE = 4`` — не пропускная способность, а предохранитель.
При 3 запросах в минуту одновременность около 0.075, так что четвёртый
одновременный запрос — это уже патология, и правильная реакция на неё —
быстрый ``503``, а не очередь, в которой все дождутся истечения бюджета.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mktlink.api.errors import http_for
from mktlink.api.routes import Deps, handle
from mktlink.api.schemas import MetaBlock, ProductRequest, ProductResponse, UrlBlock
from mktlink.constants import GLOBAL_SEMAPHORE, RETRY_AFTER_SHED_S
from mktlink.settings import Settings


def create_app(
    deps: Deps,
    settings: Settings | None = None,
    admission: Any | None = None,
) -> Any:
    from fastapi import FastAPI, Header, Query, Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    cfg = settings or Settings()
    gate = asyncio.Semaphore(GLOBAL_SEMAPHORE)

    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="mktlink",
        summary="Название и продавец по ссылке маркетплейса",
        lifespan=lifespan,
    )

    def _rid(header: str | None) -> str:
        return header or uuid.uuid4().hex

    def _deny(status: str, rid: str, url: str, retry_after: int | None) -> JSONResponse:
        body = ProductResponse(
            status=status,  # type: ignore[arg-type]
            request_id=rid,
            url=UrlBlock(submitted=url),
            meta=MetaBlock(retry_after_seconds=retry_after),
        )
        headers = {"X-Request-Id": rid}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=http_for(status), content=body.model_dump(mode="json"), headers=headers
        )

    async def _serve(req: ProductRequest, rid: str, api_key: str | None = None) -> JSONResponse:
        # Требование 7. Учёт в кредитах, а не в запросах: попадание в кэш не
        # стоит ничего, холодный запрос стоит слота прокси, а принудительный
        # сброс кэша — гарантированно. Лимит, считающий их одинаково, либо
        # душит дешёвый трафик, либо пропускает дорогой.
        key = None
        if admission is not None:
            from mktlink.api.apikey import RateLimited, Unauthorized  # noqa: PLC0415

            try:
                key = admission.authenticate(api_key)
                admission.charge(key, "cold")
            except Unauthorized:
                return _deny("unauthorized", rid, req.url, None)
            except RateLimited as exc:
                return _deny("rate_limited", rid, req.url, exc.retry_after_s)

        if gate.locked():
            # Быстрый отказ вместо очереди: запрос, простоявший в очереди,
            # всё равно не успеет, и лучше сказать это сразу.
            body = ProductResponse(
                status="capacity_exhausted",
                request_id=rid,
                url=UrlBlock(submitted=req.url),
                meta=MetaBlock(reason="capacity", retry_after_seconds=RETRY_AFTER_SHED_S),
            )
            return JSONResponse(
                status_code=http_for("capacity_exhausted"),
                content=body.model_dump(mode="json"),
                headers={"Retry-After": str(RETRY_AFTER_SHED_S)},
            )

        async with gate:
            code, body = await handle(req, deps, request_id=rid)

        if admission is not None and key is not None:
            # Возврат переплаты: списываем по худшему сценарию ДО работы,
            # иначе абьюзер, чьи запросы всегда падают, не платит ничего.
            actual = {
                "ok": "cache_hit" if body.meta.cache == "hit" else "cold",
                "stale": "stale",
                "invalid_url": "invalid_url",
                "host_not_allowed": "unsupported_marketplace",
                "not_a_product_url": "invalid_url",
            }.get(body.status, "cold")
            admission.refund(key, 10, actual)

        headers = {"X-Request-Id": rid}
        if body.meta.retry_after_seconds is not None:
            headers["Retry-After"] = str(body.meta.retry_after_seconds)
        if body.status in ("ok", "stale"):
            headers["Cache-Control"] = "no-store"
        return JSONResponse(
            status_code=code, content=body.model_dump(mode="json"), headers=headers
        )

    @app.post("/v1/product")
    async def post_product(
        req: ProductRequest,
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        return await _serve(req, _rid(x_request_id), x_api_key)

    @app.get("/v1/product")
    async def get_product(
        url: str = Query(...),
        max_wait_ms: int | None = Query(default=None),
        max_stale_s: int = Query(default=900),
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        req = ProductRequest(url=url, max_wait_ms=max_wait_ms, max_stale_s=max_stale_s)
        return await _serve(req, _rid(x_request_id), x_api_key)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        # Готовность — это «можем ли обслужить», а не «процесс жив».
        return {
            "ok": True,
            "budget_ms": cfg.response_budget_ms,
            "client_timeout_hint_ms": cfg.client_timeout_hint_ms,
        }

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        rid = uuid.uuid4().hex
        body = ProductResponse(
            status="capacity_exhausted",
            request_id=rid,
            url=UrlBlock(submitted=""),
            meta=MetaBlock(reason="internal"),
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    return app
