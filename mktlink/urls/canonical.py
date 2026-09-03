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
    offer = tuple(
        sorted(
            (k, v)
            for k, v in parse_qsl(query, keep_blank_values=False)
            if k.lower() in keep and not _is_noise(k)
        )
    )
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
