"""Раскрутка на стороне scrape.do с тем же сокращением Ozon, что у карточки.

Поставщик следует редиректам и для Ozon выполняет JS. Конечный адрес берём
из его заголовка, а не из ссылок в HTML. Промежуточные переходы происходят
в сети поставщика; наш сервер не открывает возвращённый адрес.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from mktlink.constants import RESOLVE_BUDGET_MS
from mktlink.egress.scrapedo import ScrapeDoError, ScrapeDoTransport
from mktlink.timing.deadline import Deadline, stage
from mktlink.urls.redirects import CrossedMarketplace, NotARedirect
from mktlink.urls.registry import UnknownHost, match_path, rule_for_host
from mktlink.urls.ssrf import UnwindChallenged, check_path_veto
from mktlink.urls.validate import UrlRejected, validate


@dataclass
class ScrapeDoResolver:
    transport: ScrapeDoTransport
    sender: Any = None

    async def __call__(self, dl: Deadline, url: str, mp: str) -> tuple[str, int]:
        _check_marketplace(url, mp)
        final_url = None

        async def capture(request_url: str, *, headers: dict[str, str], timeout_ms: int):
            nonlocal final_url
            status, body, response_headers = await self._send(request_url, headers, timeout_ms)
            headers_lower = {k.lower(): v for k, v in response_headers.items()}
            final_url = headers_lower.get("scrape.do-resolved-url")
            return status, body

        # Копия на запрос: заголовок одного запроса не может попасть в соседний.
        transport = replace(self.transport, sender=capture)
        async with stage(dl, "unwind_api", cap_ms=RESOLVE_BUDGET_MS, reserve_ms=0) as budget:
            status, _ = await transport(url, headers={}, proxy=None, impersonate=None,
                                        timeout_ms=budget)
        if status in (403, 429):
            raise UnwindChallenged(url, egress="api")
        if status >= 400:
            raise ScrapeDoError("provider could not resolve the link")
        if not final_url:
            raise ScrapeDoError("provider omitted resolved URL")
        parsed = _check_marketplace(final_url, mp)
        check_path_veto(mp, parsed.path, egress="api", url=final_url)
        if not match_path(parsed.host, parsed.path).is_pdp:
            raise NotARedirect("provider did not reach a product URL")
        # Один наблюдаемый переход short -> product. Внутренние хопы поставщик
        # не сообщает; они не участвуют в локальном планировщике редиректов.
        return final_url, 1

    async def _send(self, url: str, headers: dict[str, str], timeout_ms: int):
        from curl_cffi.requests import AsyncSession  # noqa: PLC0415
        from curl_cffi.requests.exceptions import RequestException, Timeout  # noqa: PLC0415

        try:
            if self.sender is not None:
                return await self.sender(url, headers=headers, timeout_ms=timeout_ms)
            async with AsyncSession(trust_env=False) as session:
                response = await session.get(url, headers=headers, timeout=timeout_ms / 1000,
                                             allow_redirects=False)
                return response.status_code, response.text, dict(response.headers)
        except Timeout:
            # ScrapeDoTransport переводит в DeadlineExceeded.
            raise
        except RequestException:
            raise ScrapeDoError("provider transport failed") from None


def _check_marketplace(url: str, mp: str):
    try:
        parsed = validate(url)
    except UrlRejected:
        raise CrossedMarketplace("provider returned an invalid URL") from None
    try:
        rule = rule_for_host(parsed.host)
    except UnknownHost:
        raise CrossedMarketplace("provider URL left the allowlist") from None
    if rule.marketplace != mp:
        raise CrossedMarketplace("provider URL changed marketplace")
    return parsed
