"""Сокращение целевой ссылки перед подачей в скрейпинг-API.

Зачем это существует, без эвфемизмов
------------------------------------

Скрейпинг-API scrape.do держит **блок-лист целевых доменов** для бесплатного
тарифа. Замер 2026-09-03, прямой запрос карточки Ozon::

    {"StatusCode":400,"Message":["We disabled the target domain for free
     packages. Please upgrade your account or contact with us for trial."]}

Если подать вместо адреса Ozon короткую ссылку, проверка на входе видит домен
сокращалки, не находит его в списке и пропускает запрос; редирект
разворачивается уже внутри их инфраструктуры. Так и получен единственный
успешный замер: полная карточка, название и продавец.

**Это обход тарифного ограничения, а не техническое решение, и модуль обязан
называть это своим именем.** Три следствия, каждое проверено, а не
предположено:

1. **Обход виден поставщику.** Он сам возвращает заголовок
   ``scrape.do-target-url`` с полным разрешённым адресом ``www.ozon.ru/...``,
   а при отказе следовать редиректу печатает его в тексте ошибки вместе с
   параметром ``__rr=1``. То есть обнаружение стоит одной строки в их логах,
   а цена — отключённый аккаунт в произвольный момент.
2. **Блок-лист поддерживается активно.** ``tinyurl.com`` в нём УЖЕ ЕСТЬ:
   через него приходит тот же отказ «disabled the target domain», что и через
   Ozon напрямую. Значит рабочие сокращалки — это те, которые пока не
   добавили, и срок жизни схемы не наш.
3. **Легальный путь известен и дешёвый.** Формулировка «for free packages»
   означает, что на платном тарифе домен открыт по прямому URL; у поставщика
   на каждом платном тарифе есть бесплатный триал. Поэтому переключатель
   :data:`mktlink.settings` ``shorten_via="none"`` — не задел на будущее, а
   рабочий режим, в который надо вернуться при первой возможности.

Замеренные сокращалки
---------------------

======================  =============  ==========================================
провайдер               пропускается   замечание
======================  =============  ==========================================
``clck.ru`` (Яндекс)    да             один GET без авторизации; 9.1 с до карточки
``goo.su``              да             нужен CSRF и сессия; 47 с до карточки
``tinyurl.com``         **нет**        уже в блок-листе поставщика
``is.gd`` / ``v.gd``    —              отказывают на самом создании ссылки
======================  =============  ==========================================

Основной — ``clck.ru``: у него нет ни авторизации, ни CSRF, и путь вчетверо
короче. Оговорка про него: он отдаёт не прямой редирект, а промежуточный
``sba.yandex.ru/redirect?url=…``, то есть хопов на один больше, чем кажется.
Для scrape.do это безразлично (он следует цепочке при
``X-Rnet-Allow-Redirects``), но время оттуда же.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import quote

#: Провайдеры, которых поставщик пока пропускает. Порядок — приоритет.
PROVIDERS: Final[tuple[str, ...]] = ("clck", "goo")

_CLCK_API: Final[str] = "https://clck.ru/--?url="
_GOO_HOME: Final[str] = "https://goo.su/"
_GOO_API: Final[str] = "https://goo.su/frontend-api/convert"

#: Форма короткой ссылки в ответе. Проверяется, потому что обе сокращалки
#: отдают ошибку тем же кодом 200 и обычным текстом: ``is.gd`` на том же
#: замере вернул ``Error, database insert failed`` со статусом 200, и без
#: проверки формы эта строка ушла бы в скрейпинг-API как «адрес».
_SHORT_URL: Final[re.Pattern[str]] = re.compile(r"^https://[a-z0-9.\-]+/[A-Za-z0-9_\-]{3,32}$")

_CSRF_META: Final[re.Pattern[str]] = re.compile(
    r'<meta\s+name="csrf-token"\s+content="([A-Za-z0-9]{20,})"', re.I
)


class ShortenFailed(RuntimeError):
    """Ссылку сократить не удалось.

    Отдельный тип нужен, чтобы вызывающий мог отличить «сокращалка молчит» от
    «маркетплейс отказал»: за первое штрафовать адрес нельзя.
    """


async def shorten(
    url: str,
    *,
    provider: str = "clck",
    timeout_ms: int = 4_000,
    sender: object | None = None,
) -> str:
    """Сократить ``url``. Возвращает короткую ссылку или бросает.

    Срок передаётся числом, а не :class:`~mktlink.timing.deadline.Deadline`,
    намеренно: сокращение вызывается из транспорта, а транспорт по своему
    протоколу дедлайна не получает — он получает уже нарезанный остаток.
    Тянуть дедлайн глубже, чем он объявлен, значит завести второй источник
    истины о времени.

    ``sender`` инъектируется тестами; в бою используется ``curl_cffi``.
    """
    if timeout_ms <= 0:
        raise ShortenFailed("no time left to shorten")
    if provider == "clck":
        return await _clck(url, timeout_ms, sender)
    if provider == "goo":
        return await _goo(url, timeout_ms, sender)
    raise ShortenFailed(f"unknown shortener {provider!r}")


async def _clck(url: str, timeout_ms: int, sender: object | None) -> str:
    status, body = await _get(_CLCK_API + quote(url, safe=""), timeout_ms, sender)
    short = (body or "").strip()
    if status != 200 or not _SHORT_URL.match(short):
        raise ShortenFailed(f"clck.ru: HTTP {status}, {short[:120]!r}")
    return short


async def _goo(url: str, timeout_ms: int, sender: object | None) -> str:
    """Двухшаговый: главная отдаёт CSRF и сессию, затем POST.

    Держится как резерв, а не как основной путь: лишний запрос, лишняя
    сессия и защита DDoS-Guard на входе, которая может ответить челленджем
    в любой момент.
    """
    status, home = await _get(_GOO_HOME, timeout_ms // 2, sender)
    if status != 200:
        raise ShortenFailed(f"goo.su home: HTTP {status}")
    m = _CSRF_META.search(home or "")
    if m is None:
        raise ShortenFailed("goo.su: csrf-token not found")
    status, body = await _post(
        _GOO_API,
        data=f"url={quote(url, safe='')}&alias=&is_public=1&password=",
        headers={
            "X-CSRF-TOKEN": m.group(1),
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": _GOO_HOME,
        },
        timeout_ms=timeout_ms // 2,
        sender=sender,
    )
    if status != 200:
        raise ShortenFailed(f"goo.su convert: HTTP {status}")
    m2 = re.search(r'"(https://goo\.su/[A-Za-z0-9_\-]{3,32})"', body or "")
    if m2 is None:
        raise ShortenFailed(f"goo.su: no link in {(body or '')[:120]!r}")
    return m2.group(1)


async def _get(url: str, timeout_ms: int, sender: object | None) -> tuple[int, str]:
    if sender is not None:
        return await sender(url, timeout_ms=timeout_ms)  # type: ignore[operator]
    from curl_cffi.requests import AsyncSession  # noqa: PLC0415

    async with AsyncSession(trust_env=False, impersonate="firefox147") as s:
        r = await s.get(url, timeout=timeout_ms / 1000, allow_redirects=False)
        return r.status_code, r.text


async def _post(
    url: str,
    *,
    data: str,
    headers: dict[str, str],
    timeout_ms: int,
    sender: object | None,
) -> tuple[int, str]:
    if sender is not None:
        return await sender(url, timeout_ms=timeout_ms, data=data, headers=headers)  # type: ignore[operator]
    from curl_cffi.requests import AsyncSession  # noqa: PLC0415

    async with AsyncSession(trust_env=False, impersonate="firefox147") as s:
        r = await s.post(
            url, data=data, headers=headers, timeout=timeout_ms / 1000, allow_redirects=False
        )
        return r.status_code, r.text


__all__ = ["PROVIDERS", "ShortenFailed", "shorten"]
