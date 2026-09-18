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

from pydantic import ValidationError

from mktlink.api.apikey import COST
from mktlink.api.errors import http_for
from mktlink.api.routes import Deps, handle, resolve
from mktlink.api.schemas import (
    MetaBlock,
    ProductRequest,
    ProductResponse,
    ResolveRequest,
    ShopMetaBlock,
    ShopRequest,
    ShopResponse,
    UrlBlock,
)
from mktlink.constants import GLOBAL_SEMAPHORE, RETRY_AFTER_SHED_S
from mktlink.settings import Settings
from mktlink.shops.service import ShopDeps, handle_shop

#: Дефолт GET-формы держится в одном месте со схемой: раньше здесь стояло
#: 900, а в схеме — своё значение, и две формы одного эндпоинта отвечали
#: по-разному на один и тот же запрос без параметра.
DEFAULT_MAX_STALE_S: int = ProductRequest.model_fields["max_stale_s"].default


def _first(exc: Any) -> dict[str, Any]:
    """Первая ошибка валидации в форме, пригодной для клиента.

    Одна, а не все: у ``ProductRequest`` три поля, и вываливать полный список
    pydantic-ошибок значит отдавать внутреннюю структуру модели наружу.
    """
    errors = exc.errors()
    if not errors:
        return {}
    first = errors[0]
    return {
        "field": ".".join(str(x) for x in first.get("loc", ())),
        "problem": first.get("msg", ""),
    }


def factory() -> Any:
    """Фабрика БЕЗ аргументов — та, которую умеет вызывать ``uvicorn --factory``.

    Существует потому, что документированная команда запуска не работала
    никогда. И README, и ``Dockerfile`` указывали на ``create_app``, а
    ``uvicorn --factory`` вызывает фабрику без аргументов::

        ERROR: Error loading ASGI app factory:
               create_app() missing 1 required positional argument: 'deps'

    То есть сервис не поднимался ни командой из инструкции, ни в контейнере.
    Нашлось это буквальным прохождением собственной инструкции — код, который
    никто не запускал документированным способом, документированным способом
    и не запускается.

    ``create_app`` остаётся принимающим зависимости: на нём стоят все тесты
    пути запроса, и именно инъекция позволяет проверять его без сети, без
    браузера и без proxy6.

    **Про авторизацию.** Она включается сама, когда в таблице ``api_key``
    появляется хотя бы один ключ, и не включается, пока их нет. Иначе выбор
    был бы между двумя плохими: либо свежая установка отвечает ``401`` на
    любой запрос (и первый запуск невозможен), либо квоты не действуют, пока
    кто-то не вспомнит про отдельный флаг. Проверка по факту наличия ключей
    делает включение наблюдаемым: добавили ключ — доступ закрылся.
    """
    from mktlink.api.wiring import build_deps, build_shop_deps  # noqa: PLC0415

    cfg = Settings()
    deps = build_deps(cfg)
    admission = None
    conn = getattr(deps.cache, "_conn", None)
    if conn is not None:
        from mktlink.api.apikey import Admission  # noqa: PLC0415

        keys = conn.execute("SELECT count(*) AS n FROM api_key").fetchone()
        if keys is not None and int(keys["n"]) > 0:
            admission = Admission(conn)
    shop_deps = build_shop_deps(cfg, conn, redis=getattr(deps.cache, "_redis", None))
    return create_app(deps, cfg, admission, shop_deps=shop_deps)


def create_app(
    deps: Deps,
    settings: Settings | None = None,
    admission: Any | None = None,
    *,
    shop_deps: ShopDeps | None = None,
) -> Any:
    from fastapi import FastAPI, Header, Query, Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    cfg = settings or Settings()
    gate = asyncio.Semaphore(GLOBAL_SEMAPHORE)
    if shop_deps is None:
        # Без явных зависимостей эндпоинт магазинов работает на кэше в памяти
        # и настоящем егрессе: тесты пути маркетплейсов его не трогают, а
        # прод собирает свои через build_shop_deps.
        from mktlink.shops.fetch import ShopFetcher  # noqa: PLC0415
        from mktlink.store.cache import ProductCache  # noqa: PLC0415

        shop_deps = ShopDeps(cache=ProductCache(), fetcher=ShopFetcher())

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

    def _deny(
        status: str,
        rid: str,
        url: str,
        retry_after: int | None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> JSONResponse:
        body = ProductResponse(
            status=status,  # type: ignore[arg-type]
            request_id=rid,
            url=UrlBlock(submitted=url),
            meta=MetaBlock(retry_after_seconds=retry_after, detail=detail or {}),
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
                # Списываем по ХУДШЕМУ сценарию до работы, поэтому
                # force_refresh обязан списаться как force_refresh, а не
                # как cold: иначе самый дорогой вход стоит как обычный.
                admission.charge(key, "force_refresh" if req.force_refresh else "cold")
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
            spent = COST["force_refresh"] if req.force_refresh else COST["cold"]
            actual = {
                # Принудительный сброс кэша не может оказаться «попаданием
                # в кэш»: он его обошёл. Возврата переплаты здесь нет.
                "ok": "force_refresh"
                if req.force_refresh
                else ("cache_hit" if body.meta.cache == "hit" else "cold"),
                "stale": "stale",
                "invalid_url": "invalid_url",
                "host_not_allowed": "unsupported_marketplace",
                "not_a_product_url": "invalid_url",
            }.get(body.status, "cold")
            admission.refund(key, spent, actual)

        headers = {"X-Request-Id": rid}
        if body.meta.retry_after_seconds is not None:
            headers["Retry-After"] = str(body.meta.retry_after_seconds)
        if body.status in ("ok", "stale"):
            headers["Cache-Control"] = "no-store"
        return JSONResponse(
            status_code=code, content=body.model_dump(mode="json"), headers=headers
        )

    async def _serve_resolve(req: ResolveRequest, rid: str, api_key: str | None) -> JSONResponse:
        """Раскрутка короткой ссылки. Дешёвый эндпоинт, и цена это отражает.

        Списывается ``redirect_unwind`` — 2 кредита против 10 за холодную
        карточку. Число не выдумано: раскрутка стоит до трёх редиректов и ни
        одного обращения к карточке, то есть ни одного кредита скрейпинг-API.
        Брать за неё как за карточку значило бы наказывать клиента ровно за то
        поведение, ради которого эндпоинт и сделан — развернуть ссылку один
        раз и больше нас не трогать.
        """
        key = None
        if admission is not None:
            from mktlink.api.apikey import RateLimited, Unauthorized  # noqa: PLC0415

            try:
                key = admission.authenticate(api_key)
                admission.charge(key, "redirect_unwind")
            except Unauthorized:
                return _deny("unauthorized", rid, req.url, None)
            except RateLimited as exc:
                return _deny("rate_limited", rid, req.url, exc.retry_after_s)

        # Гейт ёмкости здесь НЕ применяется: раскрутка не берёт ни слот
        # спейсинга, ни аренду прокси, ни кредит поставщика. Душить её вместе
        # с карточками значило бы отказывать в дешёвой операции из-за
        # перегрузки дорогой.
        code, body = await resolve(req, deps, request_id=rid)

        if admission is not None and key is not None and body.meta.cache == "hit":
            # Попадание в кэш раскрутки не стоило даже редиректа.
            admission.refund(key, 2, "cache_hit")

        headers = {"X-Request-Id": rid}
        if body.meta.retry_after_seconds is not None:
            headers["Retry-After"] = str(body.meta.retry_after_seconds)
        return JSONResponse(
            status_code=code, content=body.model_dump(mode="json"), headers=headers
        )

    async def _serve_shop(req: ShopRequest, rid: str, api_key: str | None) -> JSONResponse:
        """Карточка обычного магазина: название чая и магазина.

        Учёт тот же, что у карточки маркетплейса, — списать по худшему
        сценарию до работы и вернуть переплату по факту, — но худший
        сценарий дешевле: ``shop_cold`` — один прямой запрос с нашего адреса
        без прокси и без кредитов поставщика. Гейт ёмкости общий: сокет и
        память здесь те же, что у маркетплейсов.
        """
        key = None
        if admission is not None:
            from mktlink.api.apikey import RateLimited, Unauthorized  # noqa: PLC0415

            try:
                key = admission.authenticate(api_key)
                admission.charge(key, "shop_cold")
            except Unauthorized:
                return _deny_shop("unauthorized", rid, req.url, None)
            except RateLimited as exc:
                return _deny_shop("rate_limited", rid, req.url, exc.retry_after_s)

        if gate.locked():
            return _deny_shop(
                "capacity_exhausted", rid, req.url, RETRY_AFTER_SHED_S, reason="capacity"
            )

        async with gate:
            code, body = await handle_shop(req, shop_deps, request_id=rid)

        if admission is not None and key is not None:
            actual = {
                "ok": "cache_hit" if body.meta.cache == "hit" else "shop_cold",
                "stale": "stale",
                "invalid_url": "invalid_url",
                "not_a_product_url": "invalid_url",
                "shop_not_supported": "unsupported_marketplace",
                "host_not_allowed": "unsupported_marketplace",
            }.get(body.status, "shop_cold")
            admission.refund(key, COST["shop_cold"], actual)

        headers = {"X-Request-Id": rid}
        if body.meta.retry_after_seconds is not None:
            headers["Retry-After"] = str(body.meta.retry_after_seconds)
        if body.status in ("ok", "stale"):
            headers["Cache-Control"] = "no-store"
        return JSONResponse(
            status_code=code, content=body.model_dump(mode="json"), headers=headers
        )

    def _deny_shop(
        status: str,
        rid: str,
        url: str,
        retry_after: int | None,
        *,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> JSONResponse:
        body = ShopResponse(
            status=status,  # type: ignore[arg-type]
            request_id=rid,
            url=UrlBlock(submitted=url),
            meta=ShopMetaBlock(
                reason=reason, retry_after_seconds=retry_after, detail=detail or {}
            ),
        )
        headers = {"X-Request-Id": rid}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=http_for(status), content=body.model_dump(mode="json"), headers=headers
        )

    @app.post("/v1/shop")
    async def post_shop(
        req: ShopRequest,
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        return await _serve_shop(req, _rid(x_request_id), x_api_key)

    @app.get("/v1/shop")
    async def get_shop(
        url: str = Query(...),
        max_wait_ms: int | None = Query(default=None),
        max_stale_s: int = Query(default=ShopRequest.model_fields["max_stale_s"].default),
        force_refresh: bool = Query(default=False),
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        try:
            req = ShopRequest(
                url=url, max_wait_ms=max_wait_ms, max_stale_s=max_stale_s,
                force_refresh=force_refresh,
            )
        except ValidationError as exc:
            return _deny_shop("invalid_budget", _rid(x_request_id), url, None, detail=_first(exc))
        return await _serve_shop(req, _rid(x_request_id), x_api_key)

    @app.post("/v1/resolve")
    async def post_resolve(
        req: ResolveRequest,
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        return await _serve_resolve(req, _rid(x_request_id), x_api_key)

    @app.get("/v1/resolve")
    async def get_resolve(
        url: str = Query(...),
        max_wait_ms: int | None = Query(default=None),
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        try:
            req = ResolveRequest(url=url, max_wait_ms=max_wait_ms)
        except ValidationError as exc:
            return _deny("invalid_budget", _rid(x_request_id), url, None, detail=_first(exc))
        return await _serve_resolve(req, _rid(x_request_id), x_api_key)

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
        max_stale_s: int = Query(default=DEFAULT_MAX_STALE_S),
        x_request_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Any:
        """GET-форма. Валидация тела ловится ЗДЕСЬ, а не общим обработчиком.

        Дефект, который это закрывает, проверен живьём: ``Query`` объявлен без
        границ, а ``ProductRequest`` собирается внутри обработчика, поэтому
        ``ValidationError`` попадал в ``@app.exception_handler(Exception)`` и
        клиент получал ``500 capacity_exhausted`` — «у сервиса кончилась
        ёмкость» — вместо ``422 invalid_budget``. Оба конца диапазона давали
        одну и ту же неправду: и ``max_wait_ms=100``, и ``max_wait_ms=999999``.
        POST при этом отвечал корректно, потому что там модель разбирает
        FastAPI до входа в обработчик.
        """
        try:
            req = ProductRequest(url=url, max_wait_ms=max_wait_ms, max_stale_s=max_stale_s)
        except ValidationError as exc:
            return _deny("invalid_budget", _rid(x_request_id), url, None, detail=_first(exc))
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
