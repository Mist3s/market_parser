"""Долгоживущий Camoufox. Живёт ТОЛЬКО в forge.

Модуль импортирует ``camoufox``, поэтому в окружении ``api`` он не
устанавливается вовсе — это и есть проверяемая в CI формулировка запрета
браузера на пути запроса.

Аргументы запуска взяты из удалённого скрейпера (``841dac3``,
``retail_sources.py:565-593``) дословно, потому что это единственное, что там
было проверено практикой:

* ``block_images=True`` — картинки нам не нужны, а это половина трафика
  страницы и половина времени загрузки;
* ``i_know_what_im_doing=True`` — Camoufox иначе отказывается принимать
  часть аргументов;
* ``os=`` НЕ передаётся: пусть Camoufox ротирует отпечаток, как и задумано
  его автором. Именно поэтому наблюдённый UA пишется рядом с jar, а не
  замораживается константой.
* ``geoip=True`` — ДОБАВЛЕНО по замеру, в скрейпере его не было. Camoufox сам
  предупреждает: без него часовой пояс, локаль и WebGL берутся от машины, а
  не от страны выходного адреса, и это рассогласование — самостоятельный
  тэлл. Скрейпер работал с домашнего российского IP, где рассогласования не
  возникало; через прокси оно возникает всегда.

Браузер держится живым между минтингами: запуск стоит 2–8 секунд, и платить
их на каждый минтинг при 2–3 запросах в минуту незачем.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mktlink.timing.deadline import assert_off_request_path

#: Один браузер на процесс, один контекст в полёте. Каждый инстанс — около
#: 350 МБ, и три параллельных на маленькой машине означают OOM.
_LAUNCH_LOCK = asyncio.Lock()


def launch_kwargs(*, proxy_url: str | None, block_images: bool = True) -> dict[str, Any]:
    """Аргументы запуска. Форма из удалённого скрейпера."""
    kwargs: dict[str, Any] = {
        "headless": True,
        "block_images": block_images,
        "i_know_what_im_doing": True,
    }
    if proxy_url:
        # Отпечаток обязан соответствовать стране выходного адреса, иначе
        # браузер выдаёт часовой пояс и локаль машины, а не страны прокси.
        # Camoufox предупреждает об этом сам.
        kwargs["geoip"] = True
        # Camoufox ждёт разобранный прокси, а не URL целиком.
        from urllib.parse import urlsplit  # noqa: PLC0415

        p = urlsplit(proxy_url)
        proxy: dict[str, str] = {"server": f"http://{p.hostname}:{p.port}"}
        if p.username:
            proxy["username"] = p.username
        if p.password:
            proxy["password"] = p.password
        kwargs["proxy"] = proxy
    return kwargs


class CamoufoxBrowser:
    """Реализация протокола :class:`mktlink.mint.mint.Browser`."""

    def __init__(self, *, locale: str = "ru-RU", timezone: str = "Europe/Moscow") -> None:
        self._locale = locale
        self._timezone = timezone

    async def cookies_for(
        self, url: str, *, proxy_url: str, wait_ms: int
    ) -> tuple[str, str]:
        """Открыть страницу и снять cookie вместе с НАБЛЮДЁННЫМ UA.

        Возвращает готовый заголовок ``Cookie`` и тот User-Agent, который
        браузер реально отдал: Camoufox ротирует отпечаток на каждый запуск,
        поэтому константа здесь врала бы.
        """
        assert_off_request_path("camoufox launch")
        from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

        async with _LAUNCH_LOCK:
            browser_cm = AsyncCamoufox(**launch_kwargs(proxy_url=proxy_url))
            browser = await browser_cm.start()
            try:
                page = await browser.new_page(
                    locale=self._locale,
                    timezone_id=self._timezone,
                    viewport={"width": 1365, "height": 900},
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception:
                    # Капча или таймаут прогрева — штатный исход, и cookie при
                    # этом всё равно выставляются. Наблюдение из скрейпера:
                    # «captcha here is expected, the cookies land anyway».
                    pass
                await page.wait_for_timeout(wait_ms)

                ua = await page.evaluate("navigator.userAgent")
                cookies = await page.context.cookies()
                header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                return header, str(ua)
            finally:
                await browser.close()


async def probe_page(url: str, *, proxy_url: str, wait_ms: int) -> dict[str, Any]:
    """Разовый замер: что видит браузер на этом URL.

    Нужен для Phase 0, а не для рантайма: рантайм ходит через
    :class:`CamoufoxBrowser`, который отдаёт только cookie и UA.
    """
    assert_off_request_path("camoufox probe")
    from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

    async with _LAUNCH_LOCK:
        browser_cm = AsyncCamoufox(**launch_kwargs(proxy_url=proxy_url))
        browser = await browser_cm.start()
        try:
            page = await browser.new_page(locale="ru-RU", timezone_id="Europe/Moscow")
            status = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                status = resp.status if resp else None
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            await page.wait_for_timeout(wait_ms)
            html = await page.content()
            cookies = await page.context.cookies()
            return {
                "status": status,
                "url": page.url,
                "title": await page.title(),
                "html_bytes": len(html),
                "html": html,
                "user_agent": str(await page.evaluate("navigator.userAgent")),
                "cookie_names": sorted(c["name"] for c in cookies),
                "cookie_header": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
            }
        finally:
            await browser.close()
