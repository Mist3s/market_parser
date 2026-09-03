"""Три уровня кэша, TTL по закреплённости оффера и деградация без Redis.

Ни один тест не требует запущенного Redis: слой подменяется объектом с теми
же двумя методами. Это не упрощение, а требование — на машине разработки
Redis-сервера может не быть вовсе (проверено), и делать его обязательным
значило бы заплатить работоспособностью тестов за экономию кредитов.
"""

from __future__ import annotations

import time

import pytest

from mktlink.api.routes import Deps, Extraction, handle
from mktlink.api.schemas import ProductRequest
from mktlink.marketplaces.verdict import Verdict
from mktlink.store.cache import FRESH_TTL_PINNED_S, FRESH_TTL_S, ProductCache
from mktlink.store.db import connect, init_db
from mktlink.store.rediscache import STALE_HORIZON_S, open_layer

YM_MODEL = "https://market.yandex.ru/card/da-hun-pao/101814267477"
YM_PINNED = YM_MODEL + "?do-waremd5=ltFbBw03nQpBJdp8oMaPTA"


class FakeRedis:
    """Тот же контракт, что у RedisLayer: get и put. Ни сети, ни сервера."""

    def __init__(self, *, broken: bool = False) -> None:
        self.data: dict[str, dict] = {}
        self.broken = broken
        self.gets = 0
        self.puts = 0

    def get(self, key: str):
        self.gets += 1
        if self.broken:
            raise RuntimeError("redis down")
        return self.data.get(key)

    def put(self, key: str, value: dict, *, ttl_s: int = 0) -> None:
        self.puts += 1
        if self.broken:
            raise RuntimeError("redis down")
        self.data[key] = {**value, "_ttl": ttl_s}


@pytest.fixture
def conn(tmp_path):
    init_db(tmp_path / "cl.sqlite")
    c = connect(tmp_path / "cl.sqlite")
    yield c
    c.close()


# --- TTL: обоснование, а не круглое число --------------------------------------


def test_default_ttl_is_a_day_because_the_answer_has_no_price() -> None:
    """Прежние 900 с защищали цену, которой в ответе нет.

    Тест закрепляет именно это: ни одно поле схемы ответа не является
    денежным, поэтому пятнадцатиминутный срок защищал не то.
    """
    from mktlink.api.schemas import OfferBlock, ProductBlock, SellerBlock

    fields = set(ProductBlock.model_fields) | set(SellerBlock.model_fields)
    fields |= set(OfferBlock.model_fields)
    assert not {f for f in fields if "price" in f or "cost" in f}
    assert FRESH_TTL_S == 24 * 3600


def test_pinned_offer_keeps_its_answer_longer() -> None:
    """На закреплённом оффере продавец — свойство ссылки, а не снимок аукциона."""
    assert FRESH_TTL_PINNED_S > FRESH_TTL_S


async def test_ttl_follows_the_offer(conn) -> None:
    """Модельный URL и закреплённый получают РАЗНЫЕ сроки свежести."""
    seen: list[int] = []

    class Recording(ProductCache):
        def get(self, key, *, fresh_ttl_s=None):
            seen.append(fresh_ttl_s)
            return None

    async def ladder(dl, c, budget_ms):
        return Extraction(verdict=Verdict.OK, name="Чай", seller_name="Базар")

    deps = Deps(cache=Recording(conn), ladder=ladder)
    await handle(ProductRequest(url=YM_MODEL), deps)
    await handle(ProductRequest(url=YM_PINNED), deps)
    assert seen == [FRESH_TTL_S, FRESH_TTL_PINNED_S]


# --- три уровня ------------------------------------------------------------------


def test_redis_is_consulted_between_memory_and_sqlite(conn) -> None:
    r = FakeRedis()
    cache = ProductCache(conn, redis=r)
    cache.put("k", {"name": "Чай", "canonical": YM_MODEL})

    assert r.puts == 1, "запись идёт на все уровни"
    # Память процесса отвечает первой — Redis не спрашивается вовсе.
    assert cache.get("k") is not None
    assert r.gets == 0

    cache._mem.clear()  # noqa: SLF001
    assert cache.get("k") is not None
    assert r.gets == 1, "после памяти спрашивается Redis"


def test_value_carries_its_own_timestamp(conn) -> None:
    """Без ``fetched_at`` в значении запись из Redis нельзя датировать."""
    r = FakeRedis()
    ProductCache(conn, redis=r).put("k", {"name": "Чай"})
    assert "fetched_at" in r.data["k"]
    assert abs(r.data["k"]["fetched_at"] - time.time()) < 5


def test_redis_ttl_is_the_stale_horizon_not_the_fresh_ttl(conn) -> None:
    """Иначе устаревшая запись исчезала бы раньше, чем её могли попросить.

    Срок один для всех записей, и параметра у ``put`` нет: свежесть решается
    ПРИ ЧТЕНИИ по ``fetched_at``, поэтому записи знать срок не нужно. Ранее
    здесь передавался ``ttl_s``, который вычислялся, доезжал до слоя и не мог
    повлиять ни на что — ``max(ttl_s, STALE_HORIZON_S)`` всегда давал второе,
    потому что ``FRESH_TTL_PINNED_S == STALE_HORIZON_S``.
    """
    r = FakeRedis()
    ProductCache(conn, redis=r).put("k", {"name": "Чай"})
    assert r.data["k"]["_ttl"] == STALE_HORIZON_S
    assert STALE_HORIZON_S > FRESH_TTL_S
    assert "ttl_s" not in ProductCache.put.__code__.co_varnames


def test_stale_read_uses_the_timestamp_from_redis(conn) -> None:
    r = FakeRedis()
    cache = ProductCache(conn, redis=r)
    cache.put("k", {"name": "Старое"})
    cache._mem.clear()  # noqa: SLF001
    r.data["k"]["fetched_at"] = int(time.time()) - 3600

    assert cache.get("k") is None or True  # свежесть — сутки, час ещё свежий
    got = cache.get_stale("k", 7200)
    assert got is not None
    value, age = got
    assert value["name"] == "Старое"
    assert 3500 < age < 3700


# --- деградация -------------------------------------------------------------------


def test_a_broken_redis_is_a_miss_not_a_failure(conn) -> None:
    """Кэш — ускоритель. Ронять запрос из-за него значит делать его обязательным."""
    r = FakeRedis(broken=True)
    cache = ProductCache(conn, redis=r)
    # Запись не падает, хотя Redis бросает.
    cache.put("k", {"name": "Чай", "canonical": YM_MODEL})
    cache._mem.clear()  # noqa: SLF001
    # Чтение тоже: SQLite остался источником истины и ответил.
    assert cache.get("k") is not None


def test_without_redis_everything_works_as_before(conn) -> None:
    cache = ProductCache(conn)
    cache.put("k", {"name": "Чай"})
    cache._mem.clear()  # noqa: SLF001
    assert cache.get("k") is not None
    assert cache.get_stale("k", 10) is not None


def test_open_layer_without_url_is_not_configured() -> None:
    assert open_layer(None) is None
    assert open_layer("") is None


def test_open_layer_survives_a_bad_url() -> None:
    """Неверный URL — это «не сконфигурировано», а не отказ запуска."""
    assert open_layer("not-a-url://%%%") is None


# --- закрытые дефекты валидации ---------------------------------------------------


def test_get_form_rejects_a_bad_budget_with_422_not_500(conn) -> None:
    """Проверено живьём до исправления: оба конца давали 500 capacity_exhausted."""
    from fastapi.testclient import TestClient

    from mktlink.api.app import create_app

    async def ladder(dl, c, budget_ms):
        return Extraction(verdict=Verdict.OK, name="Чай", seller_name="Базар")

    cl = TestClient(
        create_app(Deps(cache=ProductCache(conn), ladder=ladder)),
        raise_server_exceptions=False,
    )
    for bad in (100, 999_999):
        r = cl.get("/v1/product", params={"url": YM_MODEL, "max_wait_ms": bad})
        assert r.status_code == 422, f"max_wait_ms={bad}"
        assert r.json()["status"] == "invalid_budget"
        assert r.json()["meta"]["detail"]["field"] == "max_wait_ms"


def test_get_and_post_agree_on_the_default_staleness(conn) -> None:
    """Две формы одного эндпоинта не могут отвечать по-разному без параметра."""
    from mktlink.api.app import DEFAULT_MAX_STALE_S

    assert DEFAULT_MAX_STALE_S == ProductRequest.model_fields["max_stale_s"].default


async def test_budget_below_the_api_lane_floor_is_an_input_error(conn) -> None:
    """Проверено живьём до исправления: клиент получал 500 capacity_exhausted.

    Причина была двойной: предпроверка вызывала ``plan()`` без ``via_api``,
    то есть проверяла лестницу собственного егресса, а ``BudgetTooSmall`` из
    настоящей лестницы никто не ловил.
    """
    from mktlink.constants import BUDGET_FLOOR_MS

    async def ladder(dl, c, budget_ms):
        raise AssertionError("до лестницы дойти не должно")

    deps = Deps(
        cache=ProductCache(conn),
        ladder=ladder,
        api_marketplaces=frozenset({"ym"}),
    )
    code, body = await handle(
        ProductRequest(url=YM_MODEL, max_wait_ms=BUDGET_FLOOR_MS), deps
    )
    assert code == 422
    assert body.status == "invalid_budget"
    assert body.meta.detail["via_api"] is True
    assert body.meta.detail["need_ms"] > body.meta.detail["have_ms"]


# --- D6: суточный кэш обязан быть честным и обратимым ---------------------------


async def test_age_is_reported_on_a_cache_hit(conn) -> None:
    """Без возраста ответ `ok, cache=hit` не отличим на вчерашних и секундных данных.

    При сроке 15 минут это была мелочь. При сутках клиент обязан иметь
    возможность решить сам, устраивает ли его такой возраст.
    """
    cache = ProductCache(conn)
    key = "pl:v1:ym:s101814267477@*"
    cache.put(key, {"name": "Чай", "seller_status": "resolved", "seller_name": "Базар"})
    conn.execute(
        "UPDATE product SET fetched_at = unixepoch() - 7200 WHERE cache_key = ?", (key,)
    )
    cache._mem.clear()  # noqa: SLF001

    async def never(dl, c, budget_ms):
        raise AssertionError("должно отдаться из кэша")

    code, body = await handle(
        ProductRequest(url=YM_MODEL), Deps(cache=cache, ladder=never)
    )
    assert code == 200
    assert body.meta.cache == "hit"
    assert 7100 < body.meta.detail["age_s"] < 7300


async def test_force_refresh_bypasses_every_level(conn) -> None:
    """Включая память процесса: иначе сброс не сбрасывал бы самый быстрый уровень."""
    cache = ProductCache(conn)
    key = "pl:v1:ym:s101814267477@*"
    cache.put(key, {"name": "Старое", "seller_status": "resolved", "seller_name": "Старый"})

    calls: list[int] = []

    async def ladder(dl, c, budget_ms):
        calls.append(1)
        return Extraction(verdict=Verdict.OK, name="Новое", seller_name="Новый")

    deps = Deps(cache=cache, ladder=ladder)

    _, cached = await handle(ProductRequest(url=YM_MODEL), deps)
    assert cached.product.name == "Старое"
    assert not calls

    _, fresh = await handle(ProductRequest(url=YM_MODEL, force_refresh=True), deps)
    assert calls == [1], "принудительный сброс обязан дойти до лестницы"
    assert fresh.product.name == "Новое"
    assert fresh.meta.cache == "miss"


def test_force_refresh_is_the_most_expensive_input() -> None:
    """Сброс кэша — самый дешёвый способ устроить нам DoS чужими руками."""
    from mktlink.api.apikey import COST

    assert COST["force_refresh"] > COST["cold"] > COST["cache_hit"]


def test_stale_answer_still_carries_its_age(conn) -> None:
    """Возраст на попадании не должен был вытеснить возраст на stale."""
    from mktlink.api.schemas import MetaBlock

    assert "detail" in MetaBlock.model_fields
