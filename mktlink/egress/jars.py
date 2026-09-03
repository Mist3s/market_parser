"""Cookie-jar: чтение из SQLite плюс короткий кэш в процессе.

Идея per-host cookie-jar взята из репозитория (``browser.py:105``), но ключ
другой, и это существенно. Там ключ — хост; здесь — пара
``(маркетплейс, прокси)``.

Причина прямая: cookie привязаны к IP, который их получил. Jar, снятый через
адрес A и предъявленный через адрес B, в лучшем случае мусор, в худшем —
сильный бот-сигнал: сессия, внезапно сменившая страну, выглядит хуже, чем
сессия без cookie вовсе.

``verified_at IS NULL`` означает «не опубликован». Читатель такой jar не
берёт: минтинг мог закончиться, но реплей через ``curl_cffi`` ещё не
подтверждён, а именно на реплее стоит вся ставка тёплого пути.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

#: Кэш в процессе живёт секунды: он снимает повторное чтение внутри одного
#: запроса, а не заменяет источник истины.
MEM_TTL_S = 5.0

#: Старше этого jar не берём: Ozon инвалидирует сессии, и предъявлять
#: протухшие cookie — это лишний сигнал.
MAX_AGE_S = 1800


@dataclass(frozen=True, slots=True)
class Jar:
    marketplace: str
    proxy_id: int
    cookie_header: str
    #: НАБЛЮДЁННЫЙ при минтинге, а не константа: Camoufox ротирует отпечаток
    #: на каждый запуск, и реплей замороженного UA рядом с этими cookie
    #: разрушает согласованность, ради которой jar и существует.
    user_agent: str
    firefox_major: int
    minted_at: int

    def age_s(self, now: float | None = None) -> int:
        return int((now if now is not None else time.time()) - self.minted_at)


class JarStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._mem: dict[tuple[str, int], tuple[float, Jar]] = {}

    def get(self, mp: str, proxy_id: int, *, max_age_s: int = MAX_AGE_S) -> Jar | None:
        key = (mp, proxy_id)
        now = time.time()
        cached = self._mem.get(key)
        if cached is not None and now - cached[0] <= MEM_TTL_S:
            return cached[1] if cached[1].age_s(now) <= max_age_s else None

        row = self._conn.execute(
            "SELECT cookie_header, ua, ff_major, minted_at FROM jar"
            " WHERE mp = ? AND proxy_id = ? AND verified_at IS NOT NULL",
            (mp, proxy_id),
        ).fetchone()
        if row is None:
            return None
        jar = Jar(
            marketplace=mp,
            proxy_id=proxy_id,
            cookie_header=row["cookie_header"],
            user_agent=row["ua"],
            firefox_major=int(row["ff_major"]),
            minted_at=int(row["minted_at"]),
        )
        self._mem[key] = (now, jar)
        return jar if jar.age_s(now) <= max_age_s else None

    def put_unverified(self, jar: Jar) -> None:
        """Записать свежий jar, но НЕ публиковать.

        Публикация отдельным шагом: между минтингом и подтверждением реплея
        jar существует, но читателю не виден. Иначе первый же запрос пойдёт
        с cookie, про которые мы ещё не знаем, работают ли они через
        ``curl_cffi``, — а это и есть непроверенная половина ставки.
        """
        self._conn.execute(
            "INSERT INTO jar (mp, proxy_id, cookie_header, ua, ff_major, minted_at, verified_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)"
            " ON CONFLICT(mp, proxy_id) DO UPDATE SET"
            "   cookie_header = excluded.cookie_header, ua = excluded.ua,"
            "   ff_major = excluded.ff_major, minted_at = excluded.minted_at,"
            "   verified_at = NULL",
            (
                jar.marketplace,
                jar.proxy_id,
                jar.cookie_header,
                jar.user_agent,
                jar.firefox_major,
                jar.minted_at,
            ),
        )
        self._mem.pop((jar.marketplace, jar.proxy_id), None)

    def publish(self, mp: str, proxy_id: int) -> None:
        """Отметить jar проверенным. Только после успешного реплея."""
        self._conn.execute(
            "UPDATE jar SET verified_at = unixepoch() WHERE mp = ? AND proxy_id = ?",
            (mp, proxy_id),
        )
        self._mem.pop((mp, proxy_id), None)

    def drop(self, mp: str, proxy_id: int) -> None:
        """Выбросить jar. Вызывается на первой же капче."""
        self._conn.execute("DELETE FROM jar WHERE mp = ? AND proxy_id = ?", (mp, proxy_id))
        self._mem.pop((mp, proxy_id), None)
