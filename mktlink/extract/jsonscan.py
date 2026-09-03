"""Поиск JSON-состояния, вкраплённого в разметку.

Техника взята из ``html_extractors._extract_json_objects`` репозитория —
``JSONDecoder().raw_decode`` от маркера, — но с четырьмя правками, каждая из
которых добыта разбором конкретного отказа.

1. **Вход — содержимое ``<script>``, а не текст узла.** Репозиторий сканирует
   ``node.get_text(" ", strip=True)``. Для обычного элемента ``html.parser``
   РАСКОДИРУЕТ HTML-энтити в текстовом узле, поэтому название с ``&quot;``
   внутри превращает JSON в невалидный, ``raw_decode`` тихо падает, и весь
   блок молча даёт ноль кандидатов. Плюс ``get_text(separator=" ")`` склеивает
   соседние строки через пробел, дополнительно ломая синтаксис.
2. **``raw_decode(text, idx)``, а не ``raw_decode(text[idx:])``.** Форма со
   срезом копирует хвост строки на каждой итерации: на карточке Я.Маркета с
   сорока блобами в 700 KiB это около 28 МБ копирования внутри дедлайна.
3. **Маркеров несколько.** Литерал ``'{"widgets"'`` — форма листинга; на
   карточке корень состояния другой. Плюс литерал не терпит пробела после
   открывающей скобки.
4. **Потолки.** Безлимитный цикл ``raw_decode`` — это CPU внутри бюджета,
   поэтому ограничены и число объектов, и суммарный разобранный размер.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Final

_DECODER: Final[json.JSONDecoder] = json.JSONDecoder()

#: Корни состояния, встречающиеся у наших маркетплейсов. Список, а не
#: литерал: у карточки и листинга он разный.
STATE_MARKERS: Final[tuple[str, ...]] = (
    '"widgets"',
    '"widgetStates"',
    '"collections"',
    '"initialState"',
    '"__NUXT__"',
    '"props"',
)

#: Сколько объектов максимум разбираем на одной странице.
MAX_OBJECTS: Final[int] = 64
#: Сколько байт максимум скармливаем декодеру суммарно.
MAX_TOTAL_BYTES: Final[int] = 8 * 1024 * 1024

#: Блобы вытаскиваются регексом по сырым байтам, а не полным DOM-парсом:
#: у Я.Маркета PARSE_MS = 200 против 60 у остальных именно потому, что
#: полный разбор HTML-карточки в сотни KiB стоит сотни миллисекунд.
_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(
    r"<script\b[^>]*>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE
)


def iter_script_blobs(html: str) -> Iterator[str]:
    """Содержимое каждого ``<script>`` без раскодирования энтити."""
    for m in _SCRIPT_RE.finditer(html):
        body = m.group(1)
        if body and body.strip():
            yield body


def iter_json_objects(
    text: str,
    *,
    markers: tuple[str, ...] = STATE_MARKERS,
    max_objects: int = MAX_OBJECTS,
    max_bytes: int = MAX_TOTAL_BYTES,
) -> Iterator[Any]:
    """Найти и разобрать JSON-объекты, содержащие один из маркеров.

    Сканирование идёт от позиции найденного маркера назад к ближайшей
    открывающей скобке — так же, как в репозитории, но с абсолютным индексом.
    """
    seen = 0
    consumed = 0
    for marker in markers:
        start = 0
        while seen < max_objects and consumed < max_bytes:
            hit = text.find(marker, start)
            if hit == -1:
                break
            brace = text.rfind("{", 0, hit + 1)
            if brace == -1:
                start = hit + len(marker)
                continue
            try:
                # Абсолютный индекс: без копирования хвоста.
                obj, end = _DECODER.raw_decode(text, brace)
            except ValueError:
                start = hit + len(marker)
                continue
            consumed += end - brace
            seen += 1
            yield obj
            start = max(end, hit + len(marker))


def iter_state_candidates(html: str) -> Iterator[Any]:
    """Состояние страницы: сперва блобы ``<script>``, затем сырой текст.

    Сырой текст — фолбэк на случай, когда состояние действительно лежит
    текстовым узлом (репозиторные фикстуры Я.Маркета устроены именно так, и
    отвечает ли так живая страница — открытый вопрос до пиннинга).
    """
    found = False
    for blob in iter_script_blobs(html):
        for obj in iter_json_objects(blob):
            found = True
            yield obj
    if not found:
        yield from iter_json_objects(html)


def walk(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Обход дерева с сохранением пути.

    Путь нужен не для красоты: по нему принимается решение, якорный ли это
    узел, и он же уходит в ответ как ``seller.source``.
    """
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, (*path, str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, (*path, str(i)))


def loads_maybe(raw: Any) -> Any:
    """Ozon кодирует состояния виджетов JSON-СТРОКАМИ внутри JSON.

    Наблюдение из репозитория (``ozon.py:258``): ``widgetStates`` — словарь,
    значения которого сами являются строками с JSON.
    """
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
