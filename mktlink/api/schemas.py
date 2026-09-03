"""Контракт API. Форма тела ОДНА на все статусы.

Единая форма — не эстетика. Клиент, которому при ошибке приходит другая
структура, вынужден писать две ветки разбора, и вторая всегда написана хуже.
Здесь же держится главное свойство ответа: ``url.canonical`` заполнен всегда,
когда он известен, включая ``202`` и ``503``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mktlink.constants import (
    BUDGET_FLOOR_MS,
    RESOLVE_BUDGET_MS,
    RESPONSE_BUDGET_MAX_MS,
)

Status = Literal[
    "ok",
    "stale",
    "pending",
    "not_found",
    "not_found_unconfirmed",
    "invalid_url",
    "invalid_budget",
    "host_not_allowed",
    "not_a_product_url",
    "marketplace_disabled",
    "unwind_challenged",
    "deadline_exceeded",
    "unauthorized",
    "rate_limited",
    "capacity_exhausted",
    "job_unknown",
]


class ProductRequest(BaseModel):
    """Вход. ``extra='forbid'``: лишнее поле — это опечатка, а не расширение."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(min_length=12, max_length=2048)]
    #: МОЖЕТ ТОЛЬКО ПОНИЗИТЬ бюджет. Нижняя граница — арифметический пол:
    #: ниже него лестницы не существует ни для одного маркетплейса, и принять
    #: такое значение значило бы обещать работу, которой не будет.
    max_wait_ms: Annotated[int, Field(ge=BUDGET_FLOOR_MS, le=RESPONSE_BUDGET_MAX_MS)] | None = None
    #: 0 — не отдавать устаревшее вовсе.
    #:
    #: Граница поднята с 12 часов до недели, а дефолт с 900 с до суток.
    #:
    #: Причина не в удобстве, а в цене промаха: карточка Ozon стоит 35
    #: кредитов из 1000 в месяц, то есть около 28 карточек. Потолок в 12 часов
    #: означал, что вчерашний правдивый ответ выбрасывается и покупается
    #: заново, хотя ни одного скоропортящегося поля в ответе нет — цены в
    #: схеме не существует вовсе.
    #:
    #: Неделя согласована с ``STALE_HORIZON_S``: дольше запись всё равно не
    #: живёт, и обещать больше значило бы обещать пустой ответ.
    max_stale_s: Annotated[int, Field(ge=0, le=604800)] = 86400


class ResolveRequest(BaseModel):
    """Вход эндпоинта раскрутки. Ни бюджета карточки, ни устаревания.

    ``max_stale_s`` здесь отсутствует намеренно: раскрутка кэшируется без
    срока, потому что короткая ссылка маркетплейса неизменяема. Параметр,
    который ничего не меняет, — это обещание, которого нет.
    """

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(min_length=12, max_length=2048)]
    #: Только понижение, как и у карточки. Пол — один хоп с валидацией.
    max_wait_ms: Annotated[int, Field(ge=600, le=RESOLVE_BUDGET_MS)] | None = None


class UrlBlock(BaseModel):
    submitted: str
    #: Заполнен всегда, когда известен. 504 возможен ТОЛЬКО когда раскрутка
    #: до канонического URL не дошла.
    canonical: str | None = None
    hops: int = 0


class ProductBlock(BaseModel):
    id: str | None = None
    id_kind: str | None = None
    #: Эхо обоих пространств возвращается всегда, даже когда не просили:
    #: это то, что позволяет вызывающему запинить оффер следующим запросом
    #: и получить стабильный ответ вместо снимка аукциона.
    product_id: str | None = None
    sku_id: str | None = None
    name: str | None = None


class SellerBlock(BaseModel):
    #: null при любом статусе, кроме resolved и first_party. Никогда не бренд,
    #: не производитель, не og:site_name и не имя маркетплейса.
    name: str | None = None
    #: Продавец, найденный в мета-разметке карточки, но НЕ подтверждённый как
    #: продавец запрошенного оффера. Отдельное поле, а не ``name``, и это
    #: важнее, чем кажется.
    #:
    #: ЗАМЕР 2026-09-03: карточка Я.Маркета называет продавца только в
    #: ``og:title`` («в интернет-магазине X на Яндекс Маркете»), а параметра
    #: выбора оффера в теле нет вовсе — значит отдан оффер по умолчанию. Если
    #: положить это значение в ``name``, клиент, читающий одно поле, запишет
    #: продавца ЧУЖОГО оффера и никогда об этом не узнает: ответ выглядит
    #: валидным. Если не отдавать вовсе — лейн Я.Маркета не отдаёт ничего.
    #:
    #: Отдельное имя закрывает и то и другое: наивное чтение ``name`` остаётся
    #: безопасным, а данные не теряются. Проверять надо ``status``.
    unverified_name: str | None = None
    id: str | None = None
    legal_name: str | None = None
    kind: Literal["third_party", "first_party", "unknown"] = "unknown"
    status: str = "unknown_layout"
    #: Провенанс, а не голое значение: без него нельзя отличить якорный путь
    #: от строки, случайно найденной на странице.
    source: str | None = None


class OfferBlock(BaseModel):
    ref: str | None = None
    selection: Literal["explicit", "marketplace_default", "none"] = "none"
    #: false на модельном URL — честное «это снимок аукциона, а не свойство
    #: ссылки»: завтра тот же URL может отдать другого продавца.
    stable: bool = False


class MetaBlock(BaseModel):
    source: str | None = None
    rung: str | None = None
    degraded: bool = False
    confirmations: int = 0
    budget_ms: int = 0
    elapsed_ms: int = 0
    cache: Literal["hit", "miss", "stale"] = "miss"
    reason: str | None = None
    retry_after_seconds: int | None = None
    job_id: str | None = None
    ym_region_id: int | None = None
    anchor_moved: bool = False
    #: Та же структура, что во внутреннем леджере дедлайна: постмортем
    #: читается из ответа, а не из логов.
    ledger: list[tuple[str, int]] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class ProductResponse(BaseModel):
    status: Status
    request_id: str
    url: UrlBlock
    marketplace: str | None = None
    product: ProductBlock = Field(default_factory=ProductBlock)
    seller: SellerBlock = Field(default_factory=SellerBlock)
    offer: OfferBlock = Field(default_factory=OfferBlock)
    meta: MetaBlock = Field(default_factory=MetaBlock)
