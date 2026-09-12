"""Транспорт через scrape.do. Российский резидентный адрес чужими руками.

Почему это вообще появилось
---------------------------

ЗАМЕР 2026-09-03 с датацентрового адреса вне России — то есть с двумя
независимыми минусами сразу. С такого адреса:

* Ozon обычным клиентом — ``403``, «Похоже, нет соединения», 5 221 байт,
  **2 cookie** против эталонных 15–25;
* Ozon настоящим Camoufox 152 — ``403``, «Antibot Captcha», 2 369 байт,
  **3 cookie**; страница карточки не отдаётся, поэтому гипотезу «делать
  запрос изнутри авторизованной страницы» с этого адреса проверить нельзя
  вовсе: нет страницы, внутри которой его делать;
* Я.Маркет — ``302`` на ``/showcaptcha`` одинаково с cookie и без.

Через scrape.do с ``geoCode=ru&super=true`` те же ссылки отдаются целиком.
Значит блокером был класс адреса, а не наш стек, и покупка собственного
резидентного пула — не единственный путь: можно арендовать чужой на запрос.

Два замера, которые определяют параметры лейнов
-----------------------------------------------

**Рендеринг нужен Ozon и вредит Я.Маркету.** Это не симметрия и не вкус:

============  ===============================  ===========================
маркетплейс   без ``render``                   с ``render``
============  ===============================  ===========================
Ozon          13 КБ, «Происходит               **730 927 байт, карточка**
              перенаправление» — JS-хендшейк
              не выполнен
Я.Маркет      **730 341 байт, карточка**       14 768 байт, пять маркеров
                                               капчи
============  ===============================  ===========================

То есть у Ozon данные достаются ТОЛЬКО браузером на их стороне, а Яндекс
именно на браузер и выдаёт челлендж, отдавая при этом SSR-оболочку обычному
запросу. Один флаг, противоположные знаки.

**Редиректы надо разрешать явно.** Ozon отвечает цепочкой на ``?__rr=1``, и
по умолчанию транспорт поставщика обрывает её ошибкой
``TooManyRedirects`` (их код ``310``), подсказывая в теле:
``Use X-Rnet-Allow-Redirects: 1``. Без этого заголовка карточка не приходит
никогда.

Цена и время
------------

Замерено по остатку кредитов: **Ozon с рендерингом — 25–35 кредитов**,
Я.Маркет без рендеринга — **10**. На бесплатном тарифе 1 000 кредитов в
месяц, то есть порядка 30–40 карточек Ozon.

Время: 9.1 с через ``clck.ru`` и 47 с через ``goo.su`` — при
``RESPONSE_BUDGET_MAX_MS = 15 000`` первое проходит впритык, второе не
проходит вовсе. Отсюда выбор сокращалки по умолчанию и отсюда же
:data:`OZON_FLOOR_MS`.

Чего этот модуль НЕ делает
--------------------------

Он не притворяется нашим прокси. ``egress_kind`` равен ``"api"``, а не
``"proxy"``: адрес не наш, здоровье по паре ``(маркетплейс, прокси)`` к нему
не относится, и штрафовать за его отказ купленный IPv4 было бы порчей
статистики, по которой этот IPv4 потом заменяют.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Final
from urllib.parse import quote, urlsplit

from mktlink.egress.shortener import ShortenFailed, shorten

API: Final[str] = "https://api.scrape.do/"

#: Заголовок, без которого цепочка редиректов Ozon обрывается их ошибкой 310.
ALLOW_REDIRECTS: Final[dict[str, str]] = {"X-Rnet-Allow-Redirects": "1"}

#: Параметры запроса на маркетплейс. ЗАМЕРЕНЫ, а не подобраны — см. таблицу
#: в докстроке модуля. Пустое значение означает флаг без значения.
PARAMS: Final[dict[str, dict[str, str]]] = {
    # Рендеринг обязателен: без него приходит интерстишл JS-рукопожатия.
    "ozon": {"geoCode": "ru", "super": "true", "render": "true", "customWait": "6000"},
    # Рендеринг ЗАПРЕЩЁН: с ним Яндекс отдаёт капчу вместо карточки.
    "ym": {"geoCode": "ru", "super": "true"},
    # WB работает с любого адреса напрямую за 7.70 ₽/мес и через API не идёт.
    # Запись существует, чтобы отсутствие лейна было решением, а не пропуском.
    "wb": {"geoCode": "ru", "super": "true"},
}

#: Кому сокращение НУЖНО, и это ровно те, чей домен закрыт тарифным гейтом.
#:
#: ЗАМЕР 2026-09-03, и он опровергает «сократим всё на всякий случай»:
#:
#: * ``ozon.ru`` прямым URL — ``400 We disabled the target domain``; через
#:   ``clck.ru`` — карточка целиком. Сокращение обязательно.
#: * ``market.yandex.ru`` прямым URL — **703 533 байта, ``og:url`` на месте,
#:   продавец «Чайный базар»**; через ту же ``clck.ru`` — 2 570 278 байт,
#:   заголовок «Яндекс Маркет», ``og:url`` отсутствует, ОДИННАДЦАТЬ маркеров
#:   капчи. Промежуточный хоп ``sba.yandex.ru/redirect`` уводит на общую
#:   страницу вместо карточки. Сокращение ЛОМАЕТ лейн.
#:
#: Отсюда правило: сокращение — не глобальный режим, а точечный обход одного
#: конкретного запрета. Лишнее применение стоит рабочего маркетплейса.
SHORTEN_REQUIRED: Final[frozenset[str]] = frozenset({"ozon"})

#: Замеренная цена запроса в кредитах поставщика. Нужна не для красоты:
#: бесплатный тариф — 1 000 кредитов в месяц, и без учёта расход виден только
#: постфактум, когда запросы начали отказывать.
CREDITS: Final[dict[str, int]] = {"ozon": 35, "ym": 10, "wb": 10}

#: На VPS сокращение иногда превышает 1.5 с. Даём до 4 с, но из бюджета
#: запроса вычитаем только фактически прошедшее время, а не весь потолок.
SHORTEN_CAP_MS: Final[int] = 4_000

#: Идентификатор «егресс не наш» для таблицы ``spacing``.
#:
#: Отрицательный намеренно: идентификаторы proxy6 положительны, поэтому
#: коллизия невозможна структурно, а не по договорённости. Ноль не подходит —
#: он слишком похож на «не заполнено».
#:
#: Спейсинг на этом пути НУЖЕН, хотя адрес чужой. Поставщик ротирует выходы,
#: но карточку у Ozon просим МЫ, и частота обращений к одному товару — наша
#: ответственность, а не его. К тому же каждый запрос стоит кредитов: слот
#: спейсинга здесь заодно и предохранитель от петли, которая молча съест
#: месячную квоту.
API_EGRESS_ID: Final[int] = -1

#: Пол времени на карточку Ozon: замерено 9.1 с через ``clck.ru``. Ступень с
#: таким полом не влезает в бюджет 5 с и влезает в 15 с — это и есть
#: осмысленная деградация, а не отказ.
OZON_FLOOR_MS: Final[int] = 9_000

#: Хост → маркетплейс. Транспорт по своему протоколу не получает имени
#: маркетплейса, поэтому выводит его из целевого URL. Список закрытый: чужой
#: хост в скрейпинг-API не уходит вовсе.
HOSTS: Final[dict[str, str]] = {
    "www.ozon.ru": "ozon",
    "ozon.ru": "ozon",
    "market.yandex.ru": "ym",
    "www.wildberries.ru": "wb",
    "card.wb.ru": "wb",
    "basket-01.wbbasket.ru": "wb",
}


class ScrapeDoError(RuntimeError):
    """Отказ самого поставщика, а не маркетплейса.

    Отделено намеренно: «у нас кончились кредиты» и «Ozon показал капчу» —
    разные события, и смешивать их значит искать причину не там.
    """

    reason = "provider_error"


class DomainDisabled(ScrapeDoError):
    """Целевой домен закрыт на текущем тарифе.

    Именно это возвращается на прямой запрос ``ozon.ru`` с бесплатным ключом:
    ``400`` и ``"We disabled the target domain for free packages"``. Отдельный
    тип, потому что лечится он ровно двумя способами — оплатой тарифа или
    сокращением ссылки, — и ни один из них не является повтором запроса.
    """

    reason = "provider_domain_disabled"


class ShortenerUnavailable(ScrapeDoError):
    """Обязательное сокращение не выполнено; прямой URL поставщику не отправляется."""

    reason = "shortener_unavailable"


def marketplace_of(url: str) -> str | None:
    return HOSTS.get(urlsplit(url).hostname or "")


def api_url(target: str, *, token: str, marketplace: str) -> str:
    """Собрать URL вызова поставщика."""
    parts = [f"token={quote(token, safe='')}", f"url={quote(target, safe='')}"]
    parts += [f"{k}={quote(v, safe='')}" for k, v in PARAMS[marketplace].items()]
    return API + "?" + "&".join(parts)


@dataclass(slots=True)
class ScrapeDoTransport:
    """Реализация :class:`mktlink.egress.client.Transport`.

    Подписи протокола сохранены целиком, включая ``proxy`` и ``impersonate``,
    хотя ни то ни другое здесь не применяется: адрес и отпечаток выбирает
    поставщик. Аргументы принимаются и ИГНОРИРУЮТСЯ явно, а не выкидываются
    из подписи, чтобы подмена транспорта оставалась подменой одной строки.
    """

    token: str
    #: ``"none"`` — прямой URL (легальный путь, требует платного тарифа),
    #: иначе имя провайдера из :data:`mktlink.egress.shortener.PROVIDERS`.
    shorten_via: str = "none"
    #: Потолок на сокращение. См. :data:`SHORTEN_CAP_MS`.
    shorten_cap_ms: int = SHORTEN_CAP_MS
    #: Инъекция для тестов: ``(url, headers, timeout_ms) -> (status, body)``.
    sender: Any = None
    #: Инъекция для тестов сокращалки.
    shorten_sender: Any = None
    #: Кэш коротких ссылок (:class:`~mktlink.store.shortlinks.OutboundShortlinks`
    #: или любой объект с ``get``/``put``). Без него всё работает, но каждый
    #: запрос платит за сокращение временем из своего окна — а у
    #: Ozon это разница между 200 и 202.
    shorten_cache: Any = None
    #: Атрибуция егресса. Читается :class:`~mktlink.egress.client.EgressClient`.
    egress_kind: str = field(default="api", init=False)

    async def __call__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        proxy: str | None,
        impersonate: str | None,
        timeout_ms: int,
    ) -> tuple[int, str]:
        mp = marketplace_of(url)
        if mp is None:
            # Закрытый список хостов — та же дисциплина, что в SSRF-фильтре:
            # чужой адрес во внешний сервис не уходит даже по ошибке.
            raise ScrapeDoError(f"host not allowed for scrape.do: {url!r}")

        target = url
        budget = timeout_ms
        if self.shorten_via != "none" and mp in SHORTEN_REQUIRED:
            cached = None
            if self.shorten_cache is not None:
                cached = self.shorten_cache.get(url, provider=self.shorten_via)
            if cached is not None:
                # Попадание в кэш исключает обращение к сокращателю.
                target = cached
            else:
                # Не больше потолка и не больше половины остатка: при совсем
                # маленьком сроке сокращение не должно съесть весь запрос.
                share = max(1, min(self.shorten_cap_ms, timeout_ms // 2))
                started = monotonic()
                try:
                    target = await shorten(
                        url,
                        provider=self.shorten_via,
                        timeout_ms=share,
                        sender=self.shorten_sender,
                    )
                except ShortenFailed:
                    raise ShortenerUnavailable("required URL shortening failed") from None
                else:
                    if self.shorten_cache is not None:
                        self.shorten_cache.put(url, target, provider=self.shorten_via)
                budget = max(1, timeout_ms - int((monotonic() - started) * 1000))

        req = api_url(target, token=self.token, marketplace=mp)
        status, body = await self._send(req, dict(ALLOW_REDIRECTS), max(1, budget))
        return _interpret(status, body)

    async def _send(
        self, url: str, headers: dict[str, str], timeout_ms: int
    ) -> tuple[int, str]:
        """Запрос к поставщику. Таймаут переводится в срыв дедлайна.

        Перевод обязателен, и вот почему. Замер живого прогона: карточка Ozon
        не успела за отведённые 10 126 мс, ``curl_cffi`` бросил ``Timeout``, и
        он ушёл наверх нетронутым — клиент получил ``500 capacity_exhausted``,
        то есть «у сервиса кончилась ёмкость». Это неправда сразу дважды:
        ёмкость была, а виноват наш собственный потолок ответа.
        :class:`~mktlink.timing.deadline.DeadlineExceeded` ловит
        ``run_ladder`` и превращает в ``BUDGET_EXHAUSTED`` — вердикт, который
        и означает «не успели мы», не штрафует адрес и отдаётся как ``504``.
        """
        from curl_cffi.requests.exceptions import Timeout  # noqa: PLC0415

        from mktlink.timing.deadline import DeadlineExceeded  # noqa: PLC0415

        try:
            if self.sender is not None:
                return await self.sender(url, headers=headers, timeout_ms=timeout_ms)
            from curl_cffi.requests import AsyncSession  # noqa: PLC0415

            async with AsyncSession(trust_env=False) as s:
                r = await s.get(url, headers=headers, timeout=timeout_ms / 1000)
                return r.status_code, r.text
        except Timeout as exc:
            # Перевод стоит ВОКРУГ обоих путей, включая инъектированный.
            # Иначе поведение отличалось бы между тестом и боем ровно в том
            # месте, которое тест и проверяет, — а такой тест бесполезен.
            raise DeadlineExceeded(f"scrape.do did not answer in {timeout_ms} ms") from exc


def _interpret(status: int, body: str) -> tuple[int, str]:
    """Развернуть ответ поставщика в ответ маркетплейса.

    Поставщик оборачивает чужой отказ в свой успех и наоборот, поэтому без
    этого шага классификаторы получают не тот статус, который был на самом
    деле. Три формы, все замерены:

    * ``400`` с ``"We disabled the target domain"`` — тарифный гейт. Это НЕ
      ответ маркетплейса, и отдавать его дальше как ``400`` значило бы
      сообщить, что плохая ссылка.
    * ``310`` с ``"TooManyRedirects"`` — цепочка редиректов оборвана самим
      транспортом поставщика. Тоже не ответ маркетплейса.
    * ``200`` с телом — ответ маркетплейса. Отдаётся как есть.
    """
    if status == 400 and "disabled the target domain" in body:
        raise DomainDisabled("target domain disabled by provider")
    if status == 310 or (body[:1] == "{" and "Redirect error" in body):
        raise ScrapeDoError(f"provider stopped at redirect: {body[:200]}")
    if status >= 400 and body[:1] == "{":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "Message" in payload:
            raise ScrapeDoError(f"HTTP {status}: {payload['Message']}")
    return status, body


__all__ = [
    "ALLOW_REDIRECTS",
    "API",
    "CREDITS",
    "HOSTS",
    "OZON_FLOOR_MS",
    "SHORTEN_CAP_MS",
    "SHORTEN_REQUIRED",
    "PARAMS",
    "DomainDisabled",
    "ScrapeDoError",
    "ScrapeDoTransport",
    "api_url",
    "marketplace_of",
]
