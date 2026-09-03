"""Кэш коротких ссылок, которые создаём мы сами.

Мотив целиком в бюджете, и он замерен. Ступень Ozon получает 13.5 с из 15,
карточка отдаётся 9–26 с, а полтора из выделенных уходили на повторное
сокращение ОДНОЙ И ТОЙ ЖЕ ссылки при каждом запросе. Наблюдение 11 998 мс
при окне 12 000 — успех на грани, и эти полторы секунды решают, будет ли
ответ ``200`` или ``202``.

Срока жизни у записи нет намеренно. Короткая ссылка — постоянное отображение
на постоянный URL товара; истекать здесь нечему, а TTL завёл бы регулярное
возвращение той самой платы, от которой кэш и избавляет. Если сокращалка
однажды удалит ссылку, это увидит скрейпинг-API отказом, и запись надо будет
удалить явно — :meth:`OutboundShortlinks.drop` для этого и существует.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class OutboundShortlinks:
    """Отображение «каноническая ссылка -> наша короткая»."""

    conn: sqlite3.Connection

    def get(self, canonical: str, *, provider: str) -> str | None:
        """Короткая ссылка для этого URL, если она сделана ТЕМ ЖЕ провайдером.

        Провайдер входит в условие, а не только в запись: ссылка ``clck.ru``
        и ссылка ``goo.su`` ведут себя по-разному (замер: разница во времени
        вчетверо), и отдавать одну вместо другой значило бы молча менять
        измеренные характеристики пути.
        """
        row = self.conn.execute(
            "SELECT short_url FROM outbound_shortlink WHERE canonical = ? AND provider = ?",
            (canonical, provider),
        ).fetchone()
        return str(row["short_url"]) if row is not None else None

    def put(self, canonical: str, short_url: str, *, provider: str) -> None:
        self.conn.execute(
            "INSERT INTO outbound_shortlink (canonical, short_url, provider)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(canonical) DO UPDATE SET"
            " short_url = excluded.short_url,"
            " provider = excluded.provider,"
            " created_at = unixepoch()",
            (canonical, short_url, provider),
        )
        self.conn.commit()

    def drop(self, canonical: str) -> None:
        """Забыть ссылку. Нужно, когда сокращалка её удалила."""
        self.conn.execute("DELETE FROM outbound_shortlink WHERE canonical = ?", (canonical,))
        self.conn.commit()


__all__ = ["OutboundShortlinks"]
