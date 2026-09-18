"""Конфигурация. Префикс ``MKTLINK_``, ни одного ``os.environ`` больше нигде."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mktlink.constants import (
    CACHE_FRESH_TTL_PINNED_S,
    CACHE_FRESH_TTL_S,
    CACHE_STALE_HORIZON_S,
    RESPONSE_BUDGET_DEFAULT_MS,
    RESPONSE_BUDGET_MAX_MS,
    RESPONSE_BUDGET_MIN_MS,
    SHOP_BUDGET_DEFAULT_MS,
    SHOP_BUDGET_FLOOR_MS,
    SHOP_TTL_S,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MKTLINK_",
        extra="ignore",
    )

    # --- ручка -------------------------------------------------------------
    #: Потолок ответа. Диапазон ровно тот, что назвал заказчик.
    #:
    #: Дефолт 15000, а не 5000, и вот почему. При 2–3 запросах в минуту на
    #: произвольных пользовательских ссылках попадание в продуктовый кэш
    #: близко к нулю: почти каждый запрос холодный по товару. Тёплым остаётся
    #: только jar, и он держится фоновым обновлением, а не трафиком. 15 секунд
    #: покупают не скорость обычного запроса (он и при 5000 идёт за ~0.7–1.5 с),
    #: а возможность ДОЖДАТЬСЯ идущего минтинга вместо ответа 202.
    response_budget_ms: int = Field(
        default=RESPONSE_BUDGET_DEFAULT_MS,
        ge=RESPONSE_BUDGET_MIN_MS,
        le=RESPONSE_BUDGET_MAX_MS,
    )

    # --- хранилище ----------------------------------------------------------
    db_path: Path = Field(default=Path("data/mktlink.sqlite"))
    #: Сокет к forge. Абстрактных адресов не используем: файл виден в ps и
    #: правами ограничивается, абстрактный — нет.
    forge_socket: Path = Field(default=Path("data/forge.sock"))

    # --- proxy6 -------------------------------------------------------------
    proxy6_api_key: str | None = None
    proxy6_country: str = "ru"
    #: Период закупки по умолчанию. Короткий намеренно: при ленивой модели
    #: неудачная покупка должна дёшево истечь, а не висеть оплаченной месяц.
    proxy6_period_days: int = Field(default=7, ge=1, le=90)

    # --- scrape.do ------------------------------------------------------------
    #: Ключ скрейпинг-API. Появился по замеру 2026-09-03: с нашего егресса
    #: (датацентр в Финляндии) Ozon и Я.Маркет недостижимы В ПРИНЦИПЕ, а через
    #: этот API с ``geoCode=ru&super=true`` отдаются целиком. То есть он не
    #: ускорение, а единственный проверенный способ дотянуться до двух
    #: маркетплейсов из трёх.
    scrapedo_token: str | None = None

    #: Какие маркетплейсы идут через API. WB здесь НЕТ намеренно: он работает
    #: напрямую за 7.70 ₽/мес, и гнать его через платные кредиты — выброшенные
    #: деньги при худшем времени ответа.
    scrapedo_marketplaces: tuple[str, ...] = ("ozon", "ym")

    #: Сокращать ли целевую ссылку. ``"none"`` — прямой URL, легальный путь,
    #: требует платного тарифа поставщика. ``"clck"``/``"goo"`` — обход
    #: тарифного гейта на бесплатном тарифе; почему это временно и чем
    #: рискует, подробно написано в докстроке
    #: :mod:`mktlink.egress.shortener`. Дефолт — легальный путь: обход
    #: включается явным решением, а не по забывчивости.
    scrapedo_shorten_via: str = "none"

    # --- маркетплейсы --------------------------------------------------------
    #: Регион Я.Маркета форсируется на исходящем (lr=213), поэтому в ключ кэша
    #: не входит. Если Phase 0 покажет, что Яндекс параметр игнорирует, регион
    #: переезжает в ключ и в meta как наблюдённый, а не утверждённый.
    ym_region_id: int = 213

    #: Ступень рендера Я.Маркета по умолчанию ВЫКЛЮЧЕНА: браузера на пути
    #: запроса не бывает. Включается только явным решением заказчика и стоит
    #: +5 с к p95.
    ym_render_enabled: bool = False

    # --- кэш результата --------------------------------------------------------
    #: Срок свежести ответа на МОДЕЛЬНОМ URL, секунды.
    #:
    #: В конфиге, а не константой, по просьбе заказчика: срок будет расти. И
    #: расти ему есть куда — устаревает здесь ровно одно: продавец оффера по
    #: умолчанию. Ни названия, ни идентификаторов это не касается, а цены в
    #: ответе нет вовсе.
    #:
    #: Верхняя граница согласована с ``STALE_HORIZON_S``: дольше запись всё
    #: равно не живёт в Redis, и разрешать больше значило бы обещать свежесть
    #: записи, которой уже нет.
    product_ttl_s: int = Field(default=CACHE_FRESH_TTL_S, ge=0, le=CACHE_STALE_HORIZON_S)

    #: Срок свежести, когда оффер ЗАКРЕПЛЁН в ссылке явным параметром.
    #:
    #: Отдельная ручка, потому что это другой случай, а не другое значение: на
    #: закреплённом оффере продавец — свойство ССЫЛКИ, и держать такой ответ
    #: дольше безопасно по построению. ``0`` означает «использовать то же, что
    #: для модельного URL».
    product_ttl_pinned_s: int = Field(
        default=CACHE_FRESH_TTL_PINNED_S, ge=0, le=CACHE_STALE_HORIZON_S
    )

    # --- Redis ----------------------------------------------------------------
    #: URL Redis для продуктового кэша. ОПЦИОНАЛЬНО: без него кэш работает на
    #: памяти процесса и SQLite, ровно как раньше.
    #:
    #: Нужен не для скорости — SQLite локально быстрее сети, — а ради кредитов
    #: скрейпинг-API: карточка Ozon стоит 35 из 1000 в месяц, и промах кэша
    #: тратит треть процента квоты. Общий кэш между процессами и
    #: перезапусками превращает повторный запрос в ноль.
    #:
    #: Недоступный Redis НЕ является отказом: см.
    #: :mod:`mktlink.store.rediscache`.
    redis_url: str | None = None

    # --- обычные магазины (/v1/shop) ----------------------------------------------
    #: Бюджет ответа для карточки обычного магазина. Отдельный от
    #: маркетплейсного: здесь один прямой запрос, а не лестница.
    shop_budget_ms: int = Field(
        default=SHOP_BUDGET_DEFAULT_MS, ge=SHOP_BUDGET_FLOOR_MS, le=RESPONSE_BUDGET_MAX_MS
    )
    #: Свежесть названия. Название не меняется, дефолт — неделя (горизонт).
    shop_ttl_s: int = Field(default=SHOP_TTL_S, ge=0, le=CACHE_STALE_HORIZON_S)
    #: Пускать ли хосты, которых нет в реестре. ВЫКЛЮЧЕНО по умолчанию: без
    #: allowlist'а эндпоинт — открытый прокси для чужих запросов. С флагом
    #: незнакомый хост проходит SSRF-проверку и читается общим экстрактором.
    shop_allow_unknown_hosts: bool = False
    #: Повторять ли заблокированный прямой запрос через прокси из пула.
    shop_proxy_fallback: bool = True

    # --- эксплуатация --------------------------------------------------------
    log_level: str = "INFO"
    metrics_enabled: bool = True

    @field_validator("db_path", "forge_socket")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        return v

    @property
    def client_timeout_hint_ms(self) -> int:
        """Что подсказать клиенту: его таймаут обязан быть выше нашего."""
        from mktlink.constants import CLIENT_TIMEOUT_MARGIN_MS

        return self.response_budget_ms + CLIENT_TIMEOUT_MARGIN_MS

    @field_validator("scrapedo_shorten_via")
    @classmethod
    def _known_shortener(cls, v: str) -> str:
        from mktlink.egress.shortener import PROVIDERS

        if v != "none" and v not in PROVIDERS:
            raise ValueError(f"scrapedo_shorten_via must be 'none' or one of {PROVIDERS}")
        return v

    @property
    def proxy6_configured(self) -> bool:
        return bool(self.proxy6_api_key)

    @property
    def scrapedo_configured(self) -> bool:
        return bool(self.scrapedo_token)

    def scrapedo_for(self, marketplace: str) -> bool:
        """Идёт ли этот маркетплейс через скрейпинг-API."""
        return self.scrapedo_configured and marketplace in self.scrapedo_marketplaces
