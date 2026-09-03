"""Слой Redis для продуктового кэша. Опциональный и деградирующий.

Зачем он нужен
--------------

Не для скорости. SQLite на локальном диске отвечает за десятки микросекунд, и
ускорять тут нечего. Redis нужен ради **кредитов скрейпинг-API**: карточка
Ozon стоит 35 кредитов из 1000 в месяц на бесплатном тарифе, то есть примерно
28 карточек. Промах кэша здесь — это не потерянные миллисекунды, а
потраченная треть процента месячной квоты. Общий кэш между процессами и
перезапусками превращает повторный запрос того же товара из траты в ноль.

Почему опциональный, а не обязательный
--------------------------------------

Проверено на этой машине 2026-09-03: ни ``redis-server``, ни ``redis-cli``, ни
пакета ``redis`` в окружении нет. Сделать Redis обязательным значит потребовать
запущенный сервер для 584 тестов и для локального запуска — то есть заплатить
работоспособностью разработки за оптимизацию расхода кредитов. Поэтому без
``MKTLINK_REDIS_URL`` система ведёт себя ровно как раньше: память процесса плюс
SQLite.

Почему падение Redis не должно быть отказом
-------------------------------------------

Кэш — ускоритель, а не источник истины. Источник истины — SQLite, и он остаётся
на месте. Недоступный Redis означает «промах», а промах означает «сходить в
маркетплейс», то есть штатный путь. Ронять запрос из-за недоступного
ускорителя — это превращать необязательную зависимость в обязательную задним
числом.

Отсюда :data:`SOCKET_TIMEOUT_S`: он мал намеренно. Вызовы кэша синхронны (так
устроен :class:`~mktlink.store.cache.ProductCache`, и routes.py вызывает его
напрямую), поэтому висящий Redis блокировал бы цикл событий. Жёсткий короткий
таймаут превращает «висит» в «промах» за предсказуемое время.

Про TTL и свежесть — две разные вещи
------------------------------------

В Redis запись живёт ГОРИЗОНТ УСТАРЕВАНИЯ (:data:`STALE_HORIZON_S`), а не срок
свежести. Свежесть считается по ``fetched_at`` внутри значения, потому что
``get_stale`` обязан уметь отдать старую запись с её возрастом. Если бы TTL
равнялся сроку свежести, устаревшая запись исчезала бы раньше, чем её могли
попросить, и параметр ``max_stale_s`` перестал бы работать.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from mktlink.constants import CACHE_STALE_HORIZON_S

log = logging.getLogger(__name__)

#: Сколько ждать Redis. Мало намеренно — см. докстроку модуля.
SOCKET_TIMEOUT_S: Final[float] = 0.2

#: Псевдоним. Число живёт в :mod:`mktlink.constants` рядом с обоими TTL,
#: потому что оно их верхняя граница и граница ``max_stale_s``.
STALE_HORIZON_S: Final[int] = CACHE_STALE_HORIZON_S

#: Префикс ключей. Версия в нём означает, что смена формы значения не требует
#: чистки: старые ключи просто перестают читаться и истекают сами.
NAMESPACE: Final[str] = "mktlink:v1:product:"


class RedisLayer:
    """Тонкая обёртка. Ни одна ошибка Redis не выходит за её границу."""

    def __init__(self, url: str, *, namespace: str = NAMESPACE) -> None:
        import redis  # noqa: PLC0415

        self._ns = namespace
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_S,
            socket_connect_timeout=SOCKET_TIMEOUT_S,
            # Пул не растёт бесконечно: один процесс, синхронные вызовы.
            max_connections=8,
        )
        #: Счётчики для метрики. Тихая деградация обязана быть ВИДИМОЙ, иначе
        #: она превращается в «кэш почему-то не помогает».
        self.errors = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        """Значение целиком, включая ``fetched_at``. ``None`` — промах."""
        try:
            raw = self._client.get(self._ns + key)
        except Exception as exc:  # noqa: BLE001 - см. докстроку класса
            self.errors += 1
            log.warning("redis get failed, degrading to sqlite: %s", exc)
            return None
        if raw is None:
            self.misses += 1
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # Мусор в значении — это промах, а не отказ. Ключ версионирован,
            # поэтому такое возможно только при порче, и лечится истечением.
            self.errors += 1
            return None
        if not isinstance(value, dict):
            self.errors += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any], *, ttl_s: int = STALE_HORIZON_S) -> None:
        try:
            self._client.set(
                self._ns + key,
                json.dumps(value, ensure_ascii=False),
                ex=max(1, ttl_s),
            )
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.warning("redis put failed, sqlite remains the source of truth: %s", exc)

    def drop(self, key: str) -> None:
        try:
            self._client.delete(self._ns + key)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            log.warning("redis delete failed: %s", exc)

    def ping(self) -> bool:
        """Живой ли. Для ``/readyz``, а не для пути запроса."""
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False


def open_layer(url: str | None) -> RedisLayer | None:
    """Поднять слой, если он сконфигурирован И импортируется.

    Отсутствие пакета ``redis`` трактуется как «не сконфигурировано», а не как
    ошибка запуска: иначе установка опциональной зависимости становилась бы
    обязательной для всех, кто задал URL по ошибке.
    """
    if not url:
        return None
    try:
        return RedisLayer(url)
    except ImportError:
        log.warning("MKTLINK_REDIS_URL задан, но пакет redis не установлен — работаю на SQLite")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось поднять Redis (%s) — работаю на SQLite", exc)
        return None


__all__ = ["NAMESPACE", "SOCKET_TIMEOUT_S", "STALE_HORIZON_S", "RedisLayer", "open_layer"]
