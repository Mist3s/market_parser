"""Доступ к SQLite: WAL, явные транзакции, никакого ORM.

Почему один файл SQLite, а не Postgres с Redis. При 2–3 запросах в минуту
серверная БД и кэш в памяти другого процесса — это две сетевые зависимости и
две поверхности отказа ради нагрузки, которую держит один файл. Прежняя
редакция дизайна выбирала Postgres+Redis потому, что была рассчитана на
осмысленный rps; на этом трафике тот выбор не оправдывается.

Что здесь важно и не очевидно:

* ``BEGIN IMMEDIATE`` для read-modify-write. SQLite по умолчанию берёт
  блокировку записи лениво, и два процесса, прочитавшие одну строку и оба
  решившие её обновить, получают ``SQLITE_BUSY`` на втором коммите — то есть
  ошибку вместо сериализации. Гард спейсинга и приёмка закупки обязаны быть
  ``IMMEDIATE``.
* ``synchronous`` разный по таблицам невозможен, поэтому берём ``FULL``: файл
  маленький, записей единицы в минуту, а терять запись never-renew нельзя.
* Деньги живут в целых копейках. Плавающая точка в деньгах — это ошибка,
  которая проявляется не сразу и не воспроизводится.
* ``check_same_thread=False``, и это НЕ отключение защиты. ASGI-сервер
  обслуживает запросы в потоках пула, поэтому соединение, привязанное к
  потоку создания, падает на первом же запросе — дефект, который проявляется
  только под настоящим сервером и не виден в тестах чистых функций.
  Безопасность обеспечивает сам SQLite: модуль собран в режиме SERIALIZED
  (``sqlite3.threadsafety == 3``), то есть сериализует доступ внутри себя.
  Проверяется ассертом при открытии, а не предполагается.
* Вызовы sqlite3 блокирующие и на время работы держат событийный цикл. При
  трёх запросах в минуту и локальном файле это микросекунды, и выносить их
  в пул было бы сложностью без выигрыша. Если поток вырастет на порядки,
  это первое место, куда смотреть.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Ждать освободившуюся блокировку, а не падать сразу. Пять секунд —
#: заведомо больше любой нашей транзакции.
BUSY_TIMEOUT_MS = 5000


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Открыть соединение с нужными PRAGMA.

    PRAGMA задаются на соединение, а не в схеме: ``journal_mode`` персистентен,
    остальные — нет, и молчаливая потеря ``foreign_keys`` после переоткрытия
    файла давала бы висячие ссылки.
    """
    if sqlite3.threadsafety != 3:
        raise RuntimeError(
            "sqlite3 is not built in SERIALIZED mode "
            f"(threadsafety={sqlite3.threadsafety}); cross-thread use is unsafe"
        )
    target = str(path)
    if not read_only:
        # Каталог создаём здесь, а не только в init_db: иначе любой путь,
        # идущий до инициализации, падает с невнятным «unable to open
        # database file» вместо понятного отказа.
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        conn = sqlite3.connect(
            f"file:{target}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
        )
    else:
        conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path) -> None:
    """Создать схему. Идемпотентно."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Транзакция с немедленной блокировкой записи.

    Единственный корректный режим для read-modify-write: иначе два писателя
    получают ошибку вместо сериализации.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def deferred(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Обычная транзакция — для чтений и одиночных вставок."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class SpendRejected(RuntimeError):
    """Денежный триггер отверг трату. ``cap`` — имя сработавшего потолка."""

    def __init__(self, cap: str) -> None:
        self.cap = cap
        super().__init__(f"spend refused by cap {cap}")


#: Имена потолков, которые бросает схема. Держим списком, чтобы отличить
#: наш потолок от любой другой ошибки целостности.
SPEND_CAPS = frozenset(
    {
        "TS_SKEW",
        "MIN_BUY_GAP",
        "CAP_BUYS_HOURLY",
        "CAP_BUYS_DAILY",
        "CAP_BUYS_MONTHLY",
        "CAP_RUB_MONTHLY",
    }
)


def translate_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    """Превратить срабатывание потолка в осмысленное исключение."""
    msg = str(exc)
    for cap in SPEND_CAPS:
        if cap in msg:
            return SpendRejected(cap)
    return exc


def record_spend(
    conn: sqlite3.Connection,
    *,
    kind: str,
    status: str,
    kop: int,
    version: int,
    period_days: int,
    p6_id: int | None = None,
    nonce: str | None = None,
) -> int:
    """Записать трату. ``ts`` намеренно не передаётся — его ставит БД.

    Приложение не имеет права назвать время траты: иначе часы или битый
    вызывающий обходят ``MIN_BUY_GAP`` и скользящие окна потолков.
    """
    try:
        cur = conn.execute(
            "INSERT INTO proxy_spend (kind, status, p6_id, nonce, kop, version, period_days)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, status, p6_id, nonce, kop, version, period_days),
        )
    except sqlite3.IntegrityError as exc:
        raise translate_integrity_error(exc) from exc
    return int(cur.lastrowid or 0)


def spent_kop_last_30d(conn: sqlite3.Connection) -> int:
    """Сколько денег мы РЕШИЛИ потратить за скользящие 30 дней.

    Считает только ``buy`` и ``prolong``: ``forfeit`` и ``discard`` — учёт уже
    потраченного внутри строки ``buy``, и суммировать их значило бы считать
    одни и те же рубли дважды.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(kop), 0) AS s FROM proxy_spend"
        " WHERE kind IN ('buy','prolong') AND status <> 'void'"
        "   AND ts > unixepoch() - 2592000"
    ).fetchone()
    return int(row["s"])


def set_never_renew(conn: sqlite3.Connection, p6_id: int, reason: str) -> None:
    """Пометить «никогда не продлевать».

    Пишется ДО вывода прокси из обслуживания и БЕЗ денежных операций в той же
    транзакции: денежный потолок не имеет права откатить именно эту запись.
    """
    with immediate(conn):
        conn.execute(
            "INSERT OR IGNORE INTO never_prolong (p6_id, reason) VALUES (?, ?)",
            (p6_id, reason),
        )
        conn.execute("UPDATE proxy SET never_renew = 1 WHERE p6_id = ?", (p6_id,))


def is_never_renew(conn: sqlite3.Connection, p6_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM never_prolong WHERE p6_id = ?"
        " UNION SELECT 1 FROM proxy WHERE p6_id = ? AND never_renew = 1",
        (p6_id, p6_id),
    ).fetchone()
    return row is not None


def fetchone(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def fetchall(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()
