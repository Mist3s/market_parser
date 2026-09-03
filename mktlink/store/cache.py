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
import sqlite3
import time
from collections import OrderedDict
from typing import Any

#: TTL свежей записи. Цена — свойство аукциона, а не ссылки, и держать её
#: дольше значит отдавать вчерашнего продавца как сегодняшнего.
FRESH_TTL_S = 900
LRU_SIZE = 512


class ProductCache:
    """Двухуровневый кэш: память процесса, затем SQLite."""

    def __init__(self, conn: sqlite3.Connection | None = None, *, size: int = LRU_SIZE) -> None:
        self._mem: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._size = size
        self._conn = conn

    def get(self, key: str) -> dict[str, Any] | None:
        """Только свежее. Устаревшее сюда не попадает — для него отдельный метод."""
        now = time.time()
        hit = self._mem.get(key)
        if hit is not None:
            ts, value = hit
            if now - ts <= FRESH_TTL_S:
                self._mem.move_to_end(key)
                return value
            del self._mem[key]

        row = self._row(key)
        if row is None:
            return None
        if now - float(row["fetched_at"]) > FRESH_TTL_S:
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
        row = self._row(key)
        if row is None:
            return None
        age = int(time.time() - float(row["fetched_at"]))
        if age > max_age_s:
            return None
        return self._decode(row), age

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._remember(key, value, ts=time.time())
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
        raw = row["payload"]
        return json.loads(raw) if raw else {}
