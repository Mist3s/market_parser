"""Каноникализация и ключ кэша.

Одно правило, из которого всё следует: **шум сбрасывается, параметры выбора
оффера СОХРАНЯЮТСЯ.**

На Ozon и Я.Маркете одна карточка агрегирует офферы разных продавцов, и
query-параметр выбирает, чей оффер нам покажут. Сбросить его значило бы
вернуть продавца другого оффера — молчаливая порча целостности, которую
клиент обнаружить не может, потому что ответ выглядит валидным.

Обратная ошибка тоже стоит денег: оставить в ключе UTM-метку значит завести
отдельную запись кэша на каждую рассылку и обнулить попадания.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from mktlink.urls.registry import Match, offer_params

#: Синонимы параметров выбора оффера, приводимые к одному имени.
#:
#: ЗАМЕР 2026-09-03: одна и та же карточка Я.Маркета приходит с
#: ``?do-waremd5=ltFbBw03nQpBJdp8oMaPTA`` (ссылка из веба) и с
#: ``?offerid=ltFbBw03nQpBJdp8oMaPTA`` (результат раскрутки короткой ссылки
#: ``/cc/``) — ЗНАЧЕНИЕ ОДНО И ТО ЖЕ. Без приведения это два ключа кэша на
#: один оффер: попадания теряются, и, что хуже, по ключу нельзя понять, что
#: речь об одном и том же продавце.
#:
#: Приводим к ``offerid``, потому что так называет его сам Я.Маркет в URL,
#: который отдаёт раскрутка.
#:
#: **И приведение обязано дедуплицировать.** Замечено на той же ссылке: URL,
#: который отдаёт раскрутка, МЕНЯЕТСЯ между запросами и иногда несёт оба
#: написания сразу. Без дедупликации алиас превращал их в две одинаковые
#: записи, и ключ кэша получался вида
#: ``@offerid=X&offerid=X`` — уникальный, ни с чем не совпадающий и потому
#: гарантирующий промах.
OFFER_ALIASES: dict[str, dict[str, str]] = {
    "ym": {"do-waremd5": "offerid"},
}

#: Трекинговый шум. Список закрытый: неизвестный параметр считается значимым
#: и остаётся, потому что цена ошибки асимметрична — потерять выбор оффера
#: хуже, чем продублировать запись кэша.
NOISE_PREFIXES: tuple[str, ...] = ("utm_", "yclid", "gclid", "_openstat", "from_")
NOISE_PARAMS: frozenset[str] = frozenset(
    {
        "utm",
        "referrer",
        "reffer",
        "ref",
        "advert_id",
        "sh",
        "lr",  # регион: форсируется на исходящем, поэтому в ключ не входит
        "rgn",
        "region_id",
        "clid",
        "mclid",
        "distr_type",
        "baobab_event_id",
        "cpc",
        "glfilter",
        "sponsored",
        "was_price",
        "miniapp",
        "text",
        "at",
    }
)


@dataclass(frozen=True, slots=True)
class Canonical:
    """Канонический вид ссылки и производный ключ кэша."""

    url: str
    marketplace: str
    #: Идентификаторы из пути: sku / nm / sku_id / product_id / ware_md5.
    ids: dict[str, str]
    #: Сохранённые параметры выбора оффера, отсортированные.
    offer: tuple[tuple[str, str], ...]

    @property
    def cache_key(self) -> str:
        """Ключ продуктового кэша.

        Идентификатор несёт префикс пространства: у Я.Маркета ``sku_id`` и
        ``product_id`` — РАЗНЫЕ вещи (в фикстуре репозитория одной карточке
        соответствуют productId 1159015329 и skuId "4382957723"), и склеивать
        их в одно поле значит однажды отдать данные не того объекта.
        """
        ident = _primary_ident(self.ids)
        suffix = "@" + urlencode(self.offer) if self.offer else "@*"
        return f"pl:v1:{self.marketplace}:{ident}{suffix}"


_IDENT_PREFIX = {
    "sku": "s",
    "nm": "n",
    "sku_id": "s",
    "product_id": "p",
    "ware_md5": "w",
    "code": "c",
}
#: Порядок разрешения: более специфичный идентификатор выигрывает.
_IDENT_ORDER = ("ware_md5", "sku", "nm", "sku_id", "product_id", "code")


def _primary_ident(ids: dict[str, str]) -> str:
    for name in _IDENT_ORDER:
        if name in ids:
            return f"{_IDENT_PREFIX[name]}{ids[name]}"
    raise ValueError(f"no usable identifier in {sorted(ids)}")


def _is_noise(name: str) -> bool:
    low = name.lower()
    return low in NOISE_PARAMS or low.startswith(NOISE_PREFIXES)


def canonicalise(host: str, path: str, query: str, m: Match) -> Canonical:
    """Собрать канонический URL и ключ кэша."""
    keep = offer_params(m.marketplace)
    aliases = OFFER_ALIASES.get(m.marketplace, {})
    # Словарь, а не список: после приведения синонимов одинаковые записи
    # обязаны склеиться в одну. Первое вхождение выигрывает — при конфликте
    # значений это даёт детерминированный ключ, а не зависящий от порядка
    # параметров в чужом URL.
    collected: dict[str, str] = {}
    for k, v in parse_qsl(query, keep_blank_values=False):
        low = k.lower()
        if low not in keep or _is_noise(k):
            continue
        collected.setdefault(aliases.get(low, low), v)
    offer = tuple(sorted(collected.items()))
    # Хост нормализуется к www-форме владельца: ozon.ru и www.ozon.ru — одна
    # и та же карточка, и держать два ключа кэша на неё незачем.
    canon_host = _canonical_host(host)
    tail = "?" + urlencode(offer) if offer else ""
    return Canonical(
        url=f"https://{canon_host}{path}{tail}",
        marketplace=m.marketplace,
        ids=dict(m.ids),
        offer=offer,
    )


_CANONICAL_HOST = {
    "ozon.ru": "www.ozon.ru",
    "www.ozon.ru": "www.ozon.ru",
    "wildberries.ru": "www.wildberries.ru",
    "www.wildberries.ru": "www.wildberries.ru",
    "market.yandex.ru": "market.yandex.ru",
}


def _canonical_host(host: str) -> str:
    return _CANONICAL_HOST.get(host, host)
