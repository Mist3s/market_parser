"""Боевые лейны: экстракторы, подключённые к сети, jar и спейсингу.

Здесь и только здесь встречаются чистый разбор и грязный ввод-вывод. Разбор
живёт в :mod:`.ozon`, :mod:`.wb`, :mod:`.ym` и тестируется фикстурами; сеть
живёт в :mod:`mktlink.egress`; лейн их сшивает.

Аренда прокси и слот спейсинга берутся ОДИН раз на запрос, а не на ступень:
все ступени одного запроса идут через один адрес с одним jar, иначе вторая
ступень предъявляла бы cookie, снятые не тем адресом.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from mktlink.budget import Rung
from mktlink.egress.client import EgressClient
from mktlink.egress.jars import Jar
from mktlink.marketplaces import ozon, wb, ym
from mktlink.marketplaces.base import Context, RungResult
from mktlink.marketplaces.selectors import Selectors
from mktlink.marketplaces.verdict import Verdict
from mktlink.timing.deadline import Deadline


class Lease(Protocol):
    """Арендованный на время запроса адрес с его сессией."""

    proxy_id: int
    proxy_url: str | None
    jar: Jar | None


@dataclass(slots=True)
class SimpleLease:
    proxy_id: int
    proxy_url: str | None = None
    jar: Jar | None = None


class Lane:
    """Общая часть трёх лейнов."""

    def __init__(
        self,
        marketplace: str,
        client: EgressClient,
        selectors: Selectors,
        lease: Lease,
    ) -> None:
        self.marketplace = marketplace
        self._client = client
        self._sel = selectors
        self._lease = lease

    def rung_fn(self, rung: Rung):
        handler = getattr(self, "_" + rung.name.split(".", 1)[1], None)
        if handler is None:
            raise KeyError(f"no handler for rung {rung.name}")

        async def fn(dl: Deadline, ctx: Context, cap_ms: int, prev: RungResult | None):
            return await handler(dl, ctx, cap_ms, prev, rung.name)

        return fn

    async def _get(
        self, dl: Deadline, url: str, cap_ms: int, stage_name: str, *, max_bytes: int
    ):
        """Запрос ступени.

        ``stage_name=None`` передаётся намеренно: стадию уже открыл
        ``run_ladder``, и вторая с тем же именем удвоила бы строку леджера.
        Имя оставлено в подписи, чтобы вызов читался, но клиенту не уходит.
        """
        from mktlink.budget import stage_reserve_ms  # noqa: PLC0415

        return await self._client.fetch(
            dl,
            url,
            marketplace=self.marketplace,
            cap_ms=cap_ms,
            reserve_ms=stage_reserve_ms(self.marketplace),
            jar=self._lease.jar,
            proxy_url=self._lease.proxy_url,
            stage_name=None,
            max_bytes=max_bytes,
        )


class OzonLane(Lane):
    def __init__(self, client, selectors, lease) -> None:
        super().__init__("ozon", client, selectors, lease)

    async def _composer_replay(self, dl, ctx, cap_ms, prev, name):
        sku = ctx.ids.get("sku") or ""
        url = ozon.composer_url(ozon.pdp_path(sku))
        r = await self._get(dl, url, cap_ms, name, max_bytes=4 * 1024 * 1024)
        payload = _json(r.body)
        verdict = ozon.classify_response(r.body, payload, r.status)
        if verdict is not None:
            return RungResult(verdict=verdict)
        return ozon.parse_pdp(payload, self._sel.get("ozon"), sku=sku)

    async def _pdp_html_replay(self, dl, ctx, cap_ms, prev, name):
        """Карточка как отрендеренный HTML.

        Ступень существовала и раньше, но извлекала ТОЛЬКО название из
        ``og:title`` и всегда возвращала ``PARTIAL`` — продавца она не искала
        вовсе. По замеру 2026-09-03 это была потеря на пустом месте: на
        отрендеренной странице продавец лежит рядом с названием и привязан
        двумя якорями. Разбор перенесён в :func:`ozon.parse_pdp_html`, где его
        можно проверить фикстурой живой страницы.
        """
        r = await self._get(dl, ctx.canonical_url, cap_ms, name, max_bytes=2 * 1024 * 1024)
        return ozon.parse_pdp_html(r.body, anchor_ids=set(ctx.anchor_ids), status=r.status)

    async def _api_html(self, dl, ctx, cap_ms, prev, name):
        """Единственная ступень API-лестницы: карточка целиком, один кредит.

        Тот же разбор, что у ``_pdp_html_replay``, — но отдельная ступень,
        потому что у неё другой пол и потолок (замер: 9–25 с против 1.8 с) и
        другая цена (35 кредитов поставщика против нуля).
        """
        r = await self._get(dl, ctx.canonical_url, cap_ms, name, max_bytes=4 * 1024 * 1024)
        return ozon.parse_pdp_html(r.body, anchor_ids=set(ctx.anchor_ids), status=r.status)

    async def _seller_widget(self, dl, ctx, cap_ms, prev, name):
        sku = ctx.ids.get("sku") or ""
        url = ozon.composer_url(ozon.pdp_path(sku) + "?layout_container=pdpPage2column")
        r = await self._get(dl, url, cap_ms, name, max_bytes=4 * 1024 * 1024)
        payload = _json(r.body)
        if payload is None:
            return RungResult(verdict=Verdict.SILENT_EMPTY)
        return ozon.parse_pdp(payload, self._sel.get("ozon"), sku=sku)


class WbLane(Lane):
    def __init__(self, client, selectors, lease) -> None:
        super().__init__("wb", client, selectors, lease)

    async def _card_detail(self, dl, ctx, cap_ms, prev, name):
        nm = ctx.ids.get("nm") or ""
        r = await self._get(dl, wb.card_url(nm), cap_ms, name, max_bytes=4 * 1024 * 1024)
        payload = _json(r.body)
        verdict = wb.classify_response(payload, r.status)
        if verdict is not None:
            return RungResult(verdict=verdict)
        return wb.parse_card(payload, self._sel.get("wb"), nm=nm)

    async def _pdp_html(self, dl, ctx, cap_ms, prev, name):
        nm = ctx.ids.get("nm") or ""
        r = await self._get(dl, wb.pdp_url(nm), cap_ms, name, max_bytes=2 * 1024 * 1024)
        blocked = wb.classify_response(None, r.status)
        if blocked in (Verdict.CAPTCHA, Verdict.HTTP_429, Verdict.UPSTREAM_ERROR):
            return RungResult(verdict=blocked)
        if not r.body:
            return RungResult(verdict=Verdict.SILENT_EMPTY)
        title = _og_title(r.body)
        if title is None:
            return RungResult(verdict=Verdict.SCHEMA_DRIFT)
        return RungResult(verdict=Verdict.PARTIAL, name=title)

    async def _seller_page(self, dl, ctx, cap_ms, prev, name):
        # Обогащение: юрлицо продавца. Первая ступень, которую выбрасывают при
        # нехватке бюджета — её отсутствие даёт legal_name: null, а не догадку.
        if prev is None or not prev.seller_id:
            # Обогащать нечего. Ступень пустая и диагноз не меняет.
            return RungResult(verdict=Verdict.PARTIAL)
        url = f"https://www.wildberries.ru/seller/{prev.seller_id}"
        r = await self._get(dl, url, cap_ms, name, max_bytes=1024 * 1024)
        return RungResult(verdict=Verdict.PARTIAL, legal_name=_legal_name(r.body))


class YmLane(Lane):
    def __init__(self, client, selectors, lease) -> None:
        super().__init__("ym", client, selectors, lease)

    async def _pdp_html(self, dl, ctx, cap_ms, prev, name):
        path = ctx.canonical_url.split("market.yandex.ru", 1)[-1]
        r = await self._get(dl, ym.fetch_url(path), cap_ms, name, max_bytes=2 * 1024 * 1024)
        return ym.parse_pdp(
            r.body, self._sel.get("ym"), anchor_ids=set(ctx.anchor_ids), status=r.status
        )

    async def _api_html(self, dl, ctx, cap_ms, prev, name):
        """Карточка через скрейпинг-API. Рендеринг ЗАПРЕЩЁН на стороне API.

        Причина в :data:`mktlink.egress.scrapedo.PARAMS`: с рендерингом Яндекс
        отдаёт капчу (14 768 байт, пять маркеров), без него — полную
        SSR-оболочку с названием и продавцом (703 533 байта). Здесь же и
        причина, по которой сокращать ссылку Я.Маркета нельзя.

        Регион не форсируется: ``lr=213`` уводил бы запрос на другой URL, а
        гео выбирает поставщик параметром ``geoCode=ru``.
        """
        r = await self._get(dl, ctx.canonical_url, cap_ms, name, max_bytes=4 * 1024 * 1024)
        return ym.parse_pdp(
            r.body, self._sel.get("ym"), anchor_ids=set(ctx.anchor_ids), status=r.status
        )

    async def _offers_page(self, dl, ctx, cap_ms, prev, name):
        pid = ctx.ids.get("product_id") or ctx.ids.get("sku_id") or ""
        r = await self._get(
            dl, ym.fetch_url(f"/product--x/{pid}/offers"), cap_ms, name, max_bytes=2 * 1024 * 1024
        )
        return ym.parse_pdp(
            r.body, self._sel.get("ym"), anchor_ids=set(ctx.anchor_ids), status=r.status
        )

    async def _pdp_html_retry(self, dl, ctx, cap_ms, prev, name):
        # Второй транспорт для корроборации. Тот же URL, но смысл в том, что
        # к этому моменту jar мог быть перевыпущен.
        return await self._pdp_html(dl, ctx, cap_ms, prev, name)

    async def _shop_page(self, dl, ctx, cap_ms, prev, name):
        if prev is None or not prev.seller_id:
            return RungResult(verdict=Verdict.PARTIAL)
        r = await self._get(
            dl, ym.fetch_url(f"/shop--x/{prev.seller_id}"), cap_ms, name, max_bytes=1024 * 1024
        )
        return RungResult(verdict=Verdict.PARTIAL, legal_name=_legal_name(r.body))


LANES = {"ozon": OzonLane, "wb": WbLane, "ym": YmLane}


def build_lane(mp: str, client: EgressClient, selectors: Selectors, lease: Lease) -> Lane:
    return LANES[mp](client, selectors, lease)


# --- мелкие помощники ---------------------------------------------------------


def _json(body: str) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def _og_title(html: str) -> str | None:
    """Название из ``og:title``.

    Своей регулярки здесь больше нет: она предполагала порядок атрибутов
    ``property`` → ``content`` и на Яндексе не совпадала никогда (см.
    :mod:`mktlink.extract.htmlmeta`). Чтение — общее для всех лейнов, потому
    что ошибка была общей.
    """
    from mktlink.extract.htmlmeta import meta_content  # noqa: PLC0415
    from mktlink.extract.normalize import normalize_text  # noqa: PLC0415

    og = meta_content(html, "og:title")
    return (normalize_text(og) or None) if og else None


_LEGAL = None


def _legal_name(html: str) -> str | None:
    """Юрлицо со страницы продавца.

    Ищется по организационно-правовой форме, а не по вёрстке: она переживает
    редизайн, а класс блока — нет.
    """
    global _LEGAL
    if _LEGAL is None:
        import re  # noqa: PLC0415

        _LEGAL = re.compile(r"((?:ООО|ИП|АО|ЗАО|ПАО|ОАО|НАО)\s+[«\"][^»\"]{2,80}[»\"])")
    m = _LEGAL.search(html)
    return m.group(1) if m else None
