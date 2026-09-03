"""Эталонные копии функций батч-скрейпера, чьи отказы мы исправляем.

Сам скрейпер из репозитория удалён — он делал другую работу (категорийный
листинг детского питания раз в сутки) и к этому сервису отношения не имеет.
Но два его отказа несут смысл, потому что объясняют, ПОЧЕМУ наши функции
устроены иначе, и это объяснение должно оставаться проверяемым, а не жить
в комментарии.

Копии дословные, снятые с коммита ``841dac3`` (последний перед mktlink):

* ``market_parser/stores/ozon.py:105-124`` — ``parse_ozon_widgets``;
* ``market_parser/stores/html_extractors.py:36-58`` — ``ensure_not_blocked``
  и ``_looks_like_catalog``.

Упрощены только зависимости: вместо построения ``ProductPrice`` считается,
сколько элементов прошло ФИЛЬТР КЛЮЧЕЙ, вместо исключения возвращается флаг.
Ни одно условие, от которого зависит отказ, не изменено — именно они и
проверяются.

Разница, которую надо знать: оригинал дополнительно отбрасывает элементы без
названия и цены, поэтому на синтетических данных его счёт ниже. Проверяемое
утверждение — про фильтр ключей, и на нём копия совпадает с оригиналом
дословно (сверено исполнением до удаления скрейпера).
"""

from __future__ import annotations

import json
from typing import Any

# --- копия market_parser/stores/ozon.py -------------------------------------

#: Фильтр ключей ``widgetStates``. Ровно как в оригинале.
#:
#: Отказ: обе подстроки — КАТЕГОРИЙНЫЕ. Виджет с такими именами рисует плитки
#: на листинге; на карточке товара их нет, и функция возвращает пустой список,
#: не отличая «карточку мы не понимаем» от «товаров не найдено».
def legacy_parse_ozon_widgets(payload: Any) -> int:
    """Сколько элементов прошло бы фильтр ключей репозиторного парсера."""
    widget_states = payload.get("widgetStates") if isinstance(payload, dict) else None
    if not isinstance(widget_states, dict):
        return 0
    found = 0
    for key, raw in widget_states.items():
        lowered = key.lower()
        if "tile" not in lowered and "searchresult" not in lowered:
            continue
        state = _loads(raw)
        items = state.get("items") if isinstance(state, dict) else None
        if not isinstance(items, list):
            continue
        found += len(items)
    return found


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# --- копия market_parser/stores/html_extractors.py ---------------------------

LEGACY_ANTI_BOT_MARKERS = [
    "ваш браузер не смог пройти",
    "для доступа к веб-ресурсу включите",
    "ваш ip адрес",
    "id запроса к ресурсу",
    "access denied",
    "доступ ограничен",
    "вы не робот",
    "проблемы со связью",
    "servicepipe",
    "qrator",
    "smartcaptcha",
    "antibot challenge page",
    "captcha-api.yandex",
]

#: Полевая разведка, которую стоит унаследовать: этот список — единственная
#: часть функции, которую невозможно вывести из первых принципов.
LEGACY_MARKERS_WORTH_KEEPING = tuple(LEGACY_ANTI_BOT_MARKERS)


def legacy_looks_like_catalog(html: str) -> bool:
    """Ранний выход. Ровно как в оригинале.

    Отказ живёт здесь: настоящая карточка товара содержит
    ``application/ld+json`` штатно, поэтому проверка ниже до сканирования
    маркеров просто не доходит.
    """
    return any(
        marker in html
        for marker in (
            "data-product-id",
            "application/ld+json",
            'data-testid="product',
            "/product/",
            "/good/",
            "schema.org/Product",
        )
    )


def legacy_detects_block(html: str) -> bool:
    """Признал бы репозиторный ``ensure_not_blocked`` эту страницу блоком.

    Оригинал бросает исключение; здесь возвращается флаг, потому что
    проверяется само решение, а не способ его сообщить.
    """
    if legacy_looks_like_catalog(html):
        return False
    lowered = html.lower()
    if any(marker in lowered for marker in LEGACY_ANTI_BOT_MARKERS):
        return True
    return "captcha" in lowered
