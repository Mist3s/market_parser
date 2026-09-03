"""Кэш товара: маленький LRU в процессе плюс пол в SQLite.

Размер намеренно скромный. При 2–3 запросах в минуту на произвольных
пользовательских ссылках попадание в продуктовый кэш близко к нулю — почти
каждый запрос холодный по товару. У кэша здесь две узкие работы, и обе с
коротким горизонтом:

1. Сделать повтор после ``202`` бесплатным. Горизонт — секунды.
2. Дать пол для ответа ``stale``, когда лестница не смогла.

LRU на 512 записей при 180 запросах в час — это около трёх часов истории,
то есть на порядок больше TTL. Держать 50 000 записей «на всякий случай»
здесь не за чем: это была бы память под трафик, которого нет.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import OrderedDict
from typing import Any

#: TTL свежей записи.
#:
#: **Было 900 с, стало сутки, и это исправление обоснования, а не уступка.**
#: Прежний комментарий гласил: «цена — свойство аукциона, а не ссылки, и
#: держать её дольше значит отдавать вчерашнего продавца как сегодняшнего».
#: Первая половина верна, но к нашему ответу не относится: **цены в ответе
#: нет вовсе.** ``ProductBlock`` несёт название и идентификаторы, ``SellerBlock``
#: — продавца; ни одного денежного поля в схеме не существует. Пятнадцать
#: минут защищали то, чего мы не отдаём.
#:
#: Что действительно устаревает — продавец на МОДЕЛЬНОМ URL. Это ровно то, о
#: чём предупреждает ``OfferBlock.stable``: «завтра тот же URL может отдать
#: другого продавца». Отсюда два срока вместо одного, см.
#: :data:`FRESH_TTL_PINNED_S`.
#:
#: Цена промаха при этом несимметрична и измерена: карточка Ozon стоит 35
#: кредитов из 1000 в месяц. Промах — это не потерянная секунда, а треть
#: процента месячной квоты.
FRESH_TTL_S = 24 * 3600

#: TTL, когда оффер закреплён в ссылке явным параметром.
#:
#: Тогда продавец — свойство ССЫЛКИ, а не снимок аукциона: тот же
#: ``do-waremd5`` указывает на того же продавца, пока оффер жив. Держать такую
#: запись сутками безопасно по той же логике, по которой модельный URL сутками
#: держать чуть менее безопасно. Неделя выбрана как срок, за который оффер
#: успевает исчезнуть заметным образом, а не как круглое число.
FRESH_TTL_PINNED_S = 7 * 24 * 3600

LRU_SIZE = 512

log = logging.getLogger(__name__)


class ProductCache:
    """Трёхуровневый кэш: память процесса, затем Redis, затем SQLite.

    Redis необязателен и вставлен СРЕДНИМ уровнем, а не заменяет SQLite:
    источник истины остаётся локальным, а Redis добавляет разделяемость между
    процессами и переживание перезапуска. Подробности и обоснование тихой
    деградации — в :mod:`mktlink.store.rediscache`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        size: int = LRU_SIZE,
        redis: Any = None,
        fresh_ttl_s: int = FRESH_TTL_S,
    ) -> None:
        self._mem: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._size = size
        self._conn = conn
        self._redis = redis
        self._fresh_ttl_s = fresh_ttl_s

    def get(self, key: str, *, fresh_ttl_s: int | None = None) -> dict[str, Any] | None:
        """Только свежее. Устаревшее сюда не попадает — для него отдельный метод.

        ``fresh_ttl_s`` передаётся вызывающим, потому что срок свежести зависит
        от того, закреплён ли оффер в ссылке, а это знает только он — см.
        :data:`FRESH_TTL_PINNED_S`.
        """
        ttl = self._fresh_ttl_s if fresh_ttl_s is None else fresh_ttl_s
        now = time.time()
        hit = self._mem.get(key)
        if hit is not None:
            ts, value = hit
            if now - ts <= ttl:
                self._mem.move_to_end(key)
                return value
            del self._mem[key]

        cached = self._redis_get(key)
        if cached is not None:
            ts = float(cached.get("fetched_at") or 0)
            if ts and now - ts <= ttl:
                self._remember(key, cached, ts=ts)
                return cached

        row = self._row(key)
        if row is None:
            return None
        if now - float(row["fetched_at"]) > ttl:
            return None
        value = self._decode(row)
        self._remember(key, value, ts=float(row["fetched_at"]))
        return value

    def get_stale(self, key: str, max_age_s: int) -> tuple[dict[str, Any], int] | None:
        """Устаревшее — только с явного разрешения и всегда с возрастом.

        Старый правдивый ответ лучше отсутствия ответа, но клиент обязан
        знать, что он старый: без возраста это была бы тихая подмена.
        """
        if max_age_s <= 0:
            return None
        now = time.time()
        cached = self._redis_get(key)
        if cached is not None:
            ts = float(cached.get("fetched_at") or 0)
            if ts:
                age = int(now - ts)
                if age <= max_age_s:
                    return cached, age
                # Запись есть, но слишком стара. В SQLite лежит та же
                # самая, поэтому спрашивать его повторно незачем.
                return None
        row = self._row(key)
        if row is None:
            return None
        age = int(now - float(row["fetched_at"]))
        if age > max_age_s:
            return None
        return self._decode(row), age

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Записать на все уровни.

        ``fetched_at`` кладётся В ЗНАЧЕНИЕ, а не только в колонку: Redis не
        умеет сказать, когда запись создана, а свежесть считается по возрасту.
        Без этого поля значение из Redis нельзя было бы отличить от свежего.

        **Параметра ``ttl_s`` здесь нет, и это осознанное удаление.** Он был
        добавлен вместе с суточным TTL и не мог повлиять ни на что: срок
        Redis обязан быть горизонтом устаревания, а не свежести (иначе
        ``get_stale`` не смог бы отдать старое), а сама свежесть решается
        ПРИ ЧТЕНИИ по ``fetched_at`` и ``fresh_ttl_s`` вызывающего. Так что
        записи знать срок не нужно вовсе — знать его нужно чтению. Параметр,
        который вычисляется, передаётся и ничего не меняет, хуже, чем его
        отсутствие: он выглядит работающим механизмом.
        """
        now = time.time()
        value = {**value, "fetched_at": int(now)}
        self._remember(key, value, ts=now)
        if self._redis is not None:
            from mktlink.store.rediscache import STALE_HORIZON_S  # noqa: PLC0415

            # Горизонт устаревания, один для всех записей: свежесть решает
            # чтение, а срок здесь ограничивает лишь то, как долго запись
            # ещё может быть отдана СТАРОЙ с указанием возраста.
            self._redis_put(key, value, STALE_HORIZON_S)
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO product (cache_key, mp, canonical, name, seller_name, seller_id,"
            " seller_status, payload, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, unixepoch())"
            " ON CONFLICT(cache_key) DO UPDATE SET"
            "   name = excluded.name, seller_name = excluded.seller_name,"
            "   seller_id = excluded.seller_id, seller_status = excluded.seller_status,"
            "   payload = excluded.payload, fetched_at = excluded.fetched_at",
            (
                key,
                key.split(":")[2] if key.count(":") >= 2 else "",
                value.get("canonical", ""),
                value.get("name"),
                value.get("seller_name"),
                value.get("seller_id"),
                value.get("seller_status", "unknown_layout"),
                json.dumps(value, ensure_ascii=False),
            ),
        )

    # --- внутреннее ----------------------------------------------------------

    def _redis_get(self, key: str) -> dict[str, Any] | None:
        """Чтение из слоя, которое НЕ МОЖЕТ уронить запрос.

        ``RedisLayer`` глотает свои ошибки сам, но слой инъектируемый, и
        правило «кэш — ускоритель, а не источник отказа» обязано принуждаться
        на ГРАНИЦЕ, а не внутри одной реализации. Тест с падающим слоем нашёл
        это раньше продакшена: без обёртки недоступный Redis превращал
        успешный запрос в 500.
        """
        if self._redis is None:
            return None
        try:
            value = self._redis.get(key)
        except Exception as exc:  # noqa: BLE001 - см. докстроку
            log.warning("cache layer get failed, falling through to sqlite: %s", exc)
            return None
        return value if isinstance(value, dict) else None

    def _redis_put(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        if self._redis is None:
            return
        try:
            self._redis.put(key, value, ttl_s=ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache layer put failed, sqlite still holds the truth: %s", exc)


    def _remember(self, key: str, value: dict[str, Any], *, ts: float) -> None:
        self._mem[key] = (ts, value)
        self._mem.move_to_end(key)
        while len(self._mem) > self._size:
            self._mem.popitem(last=False)

    def _row(self, key: str) -> sqlite3.Row | None:
        if self._conn is None:
            return None
        return self._conn.execute(
            "SELECT payload, fetched_at FROM product WHERE cache_key = ?", (key,)
        ).fetchone()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        """Значение из SQLite, датированное КОЛОНКОЙ, а не полем payload.

        Колонка авторитетнее: она обновляется при конфликте вставки, а
        payload — это снимок, каким его записали. Расхождение проявилось
        тестом: искусственно состаренная запись отдавала возраст, взятый из
        payload, то есть нулевой. Возвращаемое значение обязано описывать
        себя одинаково, с какого бы уровня оно ни пришло, — иначе возраст в
        ответе зависит от того, попал ли запрос в Redis или в SQLite.
        """
        raw = row["payload"]
        value: dict[str, Any] = json.loads(raw) if raw else {}
        value["fetched_at"] = int(row["fetched_at"])
        return value
