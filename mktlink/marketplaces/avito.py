"""Авито: объявление — это и есть оффер, и продавец у него ровно один.

На Ozon и Я.Маркете карточка агрегирует офферы многих продавцов, и главный
риск там — вернуть продавца не того оффера. У Авито этой развилки нет:
объявление подаёт один продавец, и ссылка на объявление закрепляет его так же
жёстко, как ``do-waremd5`` закрепляет оффер Я.Маркета. Поэтому оффер Авито —
свойство ссылки (``HostRule.listing_is_offer``), а не снимок аукциона.

Риск здесь другой — **поля-ловушки рядом с именем**. ЗАМЕР 2026-10-01 по живой
карточке 7822637882:

* ``contactBarInfo.publicProfileInfo.sellerName`` = «Частное лицо». Это ТИП
  продавца, а не имя, хотя ключ назван ровно так, как назвал бы поле с именем
  любой разработчик. Общий якорный обход по имени ключа взял бы именно его.
* ``seller.labels.nominative`` — тот же тип в другом месте.
* ``complementarySections`` — «Похожие объявления» и «Распродажа». На
  SSR-странице это только заголовки, сами объявления дорисовывает клиент, но
  полагаться на это незачем: продавец принимается только привязанным к нашему
  объявлению.

Поэтому продавец читается по ЯВНЫМ путям, а не общим обходом состояния, и
принимается, только если ``contactBarInfo.itemId`` или параметр ``iid`` ссылки
на профиль совпадает с идентификатором из URL.

Состояние страницы лежит в ``window.__staticRouterHydrationData =
JSON.parse("…")``: JSON внутри строкового литерала JS, поэтому разбирается
двумя ``json.loads``. Скрипт стоит в начале тела (символ 85 566 из 587 632),
раньше разметки объявления. Регулярка развёрнута (``[^"\\]*(?:\\.[^"\\]*)*``),
а не записана альтернацией: на живой странице это 1.3 мс против 9.1 мс.

Разметка — резерв на случай, если состояние переименуют: ``h1`` с
``data-marker="item-view/title-info"``, номер объявления в
``item-view/item-id`` и ссылка ``seller-link/link`` с тем же ``iid``.

Чего здесь нет намеренно:

* Тип продавца (частное лицо, компания) в состоянии есть, но в ответ не идёт:
  в схеме для него нет поля, а заводить поле ради одного маркетплейса —
  менять контракт для всех.
* Снятые с публикации объявления отдельно не разбираются: страница при этом
  несёт и название, и продавца, а ``closedItem`` — свойство объявления, не
  продавца.
* Скрытое имя (``hideSellerName``) не замерено. Если флаг поднят, имя не
  отдаётся, даже если оно лежит в состоянии или в разметке: продавец сам
  решил его не показывать.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from mktlink.extract.normalize import normalize_text
from mktlink.extract.seller import SellerFound, classify, make_source
from mktlink.marketplaces.base import RungResult
from mktlink.marketplaces.verdict import SellerStatus, Verdict

ORIGIN: Final[str] = "https://www.avito.ru"

#: Статус челленджа QRATOR. ЗАМЕР 2026-10-01: первый GET без cookie получает
#: именно его, с телом «Доступ ограничен: проверка безопасности». Снимается
#: proof-of-work рукопожатием в :mod:`mktlink.egress.unblock`, где то же число
#: объявлено ещё раз: egress не импортирует маркетплейсы. Равенство двух копий
#: проверяет тест.
CHALLENGE_STATUS: Final[int] = 439

#: Маркеры страницы челленджа. Нужны на случай, если челлендж придёт с другим
#: статусом: тогда его опознаёт тело. Проверено, что ни одного из них нет ни
#: на живой карточке, ни на странице снятого объявления.
CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    "доступ ограничен: проверка безопасности",
    "firewallpow",
    "pow_challenge",
    "firewall-container",
)

#: Сколько начала тела смотреть на маркеры. Страница челленджа весит 8 КБ и
#: несёт маркеры в первых сотнях байт; карточка весит 600 КБ, и сканировать её
#: целиком ради отрицательного ответа незачем.
MARKER_SCAN_CHARS: Final[int] = 16_384

#: Узел маршрута карточки в ``loaderData``.
ROUTE_NODE: Final[str] = "catalog-or-main-or-item"

#: Типы продавца, которые Авито показывает рядом с именем. Имя, совпавшее с
#: типом, — не имя. Список закрытый, и к нему добавляется подпись той же
#: страницы — ``seller.labels.nominative`` в состоянии и ``seller-info/label``
#: в разметке, — причём для ОБОИХ путей чтения имени: см. :func:`_page_kinds`.
SELLER_KINDS: Final[frozenset[str]] = frozenset({"частное лицо", "компания", "магазин"})

_STATE = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("[^"\\]*(?:\\.[^"\\]*)*")\)'
)
_H1 = re.compile(r'<h1\b[^>]*\bdata-marker="item-view/title-info"[^>]*>(.*?)</h1>', re.S)
#: ``№ 7822637882``: между знаком и числом неразрывный пробел.
_ITEM_ID = re.compile(r'data-marker="item-view/item-id"[^>]*>[^<\d]*(\d{6,12})\s*<')
_SELLER_LINK = re.compile(r'<a\b([^>]*\bdata-marker="seller-link/link"[^>]*)>(.*?)</a>', re.S)
#: ``<span data-marker="seller-info/label">Частное лицо</span>`` — тип продавца.
_SELLER_LABEL = re.compile(r'data-marker="seller-info/label"[^>]*>([^<]*)<')
_HREF = re.compile(r'\bhref="([^"]*)"')
_TAG = re.compile(r"<[^>]+>")
#: Профиль продавца: ``/brands/<id>`` у магазинов и частников с витриной,
#: ``/user/<id>/profile`` у остальных. Замерена первая форма.
_PROFILE_ID = re.compile(r"^/(?:brands|user)/([A-Za-z0-9_-]{4,64})(?:/|$)")
#: Хвост пути объявления: ``…_7822637882``.
_ITEM_TAIL = re.compile(r"_(\d{6,12})/?$")

_RESOLVED = (SellerStatus.RESOLVED, SellerStatus.FIRST_PARTY)


def is_challenge(status: int, body: str) -> bool:
    """Челлендж QRATOR: по статусу, а для страховки и по началу тела."""
    if status == CHALLENGE_STATUS:
        return True
    head = body[:MARKER_SCAN_CHARS].casefold()
    return any(m in head for m in CHALLENGE_MARKERS)


def hydration_state(html: str) -> dict[str, Any] | None:
    """Состояние гидратации или ``None``, если его нет или оно не JSON."""
    m = _STATE.search(html)
    if m is None:
        return None
    try:
        inner = json.loads(m.group(1))
        state = json.loads(inner) if isinstance(inner, str) else None
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def route_node(state: dict[str, Any]) -> dict[str, Any] | None:
    """Узел маршрута карточки.

    Сперва по имени маршрута, потом по содержимому: переименование маршрута —
    правка сборки фронтенда, а не смена данных, и терять из-за неё карточку
    незачем.
    """
    loader = state.get("loaderData")
    if not isinstance(loader, dict):
        return None
    node = loader.get(ROUTE_NODE)
    if isinstance(node, dict):
        return node
    for value in loader.values():
        if isinstance(value, dict) and ("buyerItem" in value or value.get("type") == "redirect"):
            return value
    return None


def classify_response(html: str, *, status: int = 200) -> Verdict | None:
    """Вердикт по статусу и форме тела. ``None`` — разбирать дальше."""
    if status in (401, 403) or is_challenge(status, html):
        return Verdict.CAPTCHA
    if status == 429:
        return Verdict.HTTP_429
    if status >= 500:
        return Verdict.UPSTREAM_ERROR
    if status in (301, 302, 303, 307, 308):
        # ЗАМЕР 2026-10-01: канонический адрес объявления отвечает 200 даже с
        # чужим слагом и чужим городом, а на www редиректят только m.avito.ru
        # и голый домен — но каноникализация их уже заменила. Редирект здесь
        # значит, что форма адреса перестала быть конечной; заголовок
        # ``Location`` до разбора не доезжает, поэтому идти за ним нечем.
        return Verdict.SCHEMA_DRIFT
    if status in (404, 410):
        return Verdict.NOT_FOUND
    if status != 200:
        return Verdict.UPSTREAM_ERROR
    if not html.strip():
        return Verdict.SILENT_EMPTY
    return None


def parse_pdp(html: str, *, anchor_ids: set[str], status: int = 200) -> RungResult:
    """Разобрать карточку объявления."""
    verdict = classify_response(html, status=status)
    if verdict is not None:
        return RungResult(verdict=verdict)

    state = hydration_state(html)
    node = route_node(state) if state is not None else None
    if node is not None and node.get("type") == "redirect":
        return RungResult(verdict=_redirect_verdict(node, anchor_ids))

    buyer = node.get("buyerItem") if node is not None else None
    if isinstance(buyer, dict):
        return _from_state(buyer, html, anchor_ids)
    return _from_html(html, anchor_ids)


def _redirect_verdict(node: dict[str, Any], anchor_ids: set[str]) -> Verdict:
    """Снятое объявление.

    ЗАМЕР 2026-10-01: несуществующий номер отвечает ``200``, а в состоянии
    вместо объявления лежит ``{"type": "redirect", "redirectCode": 301,
    "redirectUrl": "/kaliningrad/produkty_pitaniya"}`` — увод в категорию. Это
    положительный маркер отсутствия, а не молчание.

    Увод на адрес С ТЕМ ЖЕ номером отсутствием не является: объявление есть,
    просто адрес другой. Такого не наблюдалось, и сказать «нет» тут нельзя.
    """
    target = node.get("redirectUrl")
    path = urlsplit(target).path if isinstance(target, str) else ""
    m = _ITEM_TAIL.search(path)
    if m is not None and m.group(1) in anchor_ids:
        return Verdict.SCHEMA_DRIFT
    return Verdict.NOT_FOUND


def _from_state(buyer: dict[str, Any], html: str, anchor_ids: set[str]) -> RungResult:
    item = _dict(buyer.get("item"))
    item_id = item.get("id")
    if item_id is None:
        # Узел есть, но форма поехала: читаем разметку, а не сдаёмся.
        return _from_html(html, anchor_ids)
    if str(item_id) not in anchor_ids:
        # Карточка, но не наша: нас перебросили на другое объявление.
        return RungResult(verdict=Verdict.SCHEMA_DRIFT)

    name = _text(item.get("title")) or _h1(html)
    if _hides_name(buyer):
        # Продавец сам скрыл имя. Разметку не читаем: имя, найденное в ней,
        # было бы ровно тем, что он решил не показывать.
        return _result(name, SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, None))
    kinds = _page_kinds(html, buyer)
    seller = _seller_from_state(buyer, anchor_ids, kinds)
    if seller.status not in _RESOLVED:
        dom = _seller_from_html(html, anchor_ids, kinds)
        if dom.status in _RESOLVED:
            seller = dom
    return _result(name, seller)


def _from_html(html: str, anchor_ids: set[str]) -> RungResult:
    """Резерв без состояния: номер объявления, ``h1`` и ссылка на продавца."""
    ident = _ITEM_ID.search(html)
    name = _h1(html)
    if ident is None and name is None:
        # Ни состояния, ни разметки объявления: 200, но страница не наша.
        return RungResult(verdict=Verdict.SILENT_EMPTY)
    if ident is None or ident.group(1) not in anchor_ids:
        # Название есть, а чьё оно — доказать нечем или доказано, что чужое.
        return RungResult(verdict=Verdict.SCHEMA_DRIFT)
    return _result(name, _seller_from_html(html, anchor_ids, _page_kinds(html, None)))


def _seller_from_state(
    buyer: dict[str, Any], anchor_ids: set[str], kinds: frozenset[str]
) -> SellerFound:
    """Продавец по явным путям состояния, привязанный к объявлению."""
    bar = _dict(buyer.get("contactBarInfo"))
    profile = _dict(bar.get("publicProfileInfo"))
    links = [
        link
        for link in (
            _dict(bar.get("seller")).get("profileUrl"),
            profile.get("publicProfileLink"),
            _dict(profile.get("publicProfile")).get("link"),
            _dict(buyer.get("publicProfile")).get("link"),
            _dict(buyer.get("favoriteSeller")).get("publicProfileLink"),
        )
        if isinstance(link, str) and link
    ]
    anchored = str(bar.get("itemId")) in anchor_ids or any(
        anchor_ids.intersection(_iids(link)) for link in links
    )
    if not anchored:
        return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, None)

    seller_id = next((sid for sid in map(_profile_id, links) if sid), None)

    # ``publicProfileInfo.sellerName`` в списке нет намеренно: это тип
    # продавца, см. докстроку модуля.
    for path, value in (
        (("buyerItem", "contactBarInfo", "seller", "name"), _dict(bar.get("seller")).get("name")),
        (("buyerItem", "seller", "name"), _dict(buyer.get("seller")).get("name")),
        (
            ("buyerItem", "contactBarInfo", "publicProfileInfo", "itemSellerName"),
            profile.get("itemSellerName"),
        ),
    ):
        name = _text(value)
        if not name or name.casefold() in kinds:
            continue
        return classify(
            "avito",
            name,
            make_source("avito", "state", path),
            {"seller_id": seller_id} if seller_id else None,
        )
    return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, None)


def _seller_from_html(html: str, anchor_ids: set[str], kinds: frozenset[str]) -> SellerFound:
    """Ссылка на профиль продавца с ``iid`` нашего объявления."""
    for m in _SELLER_LINK.finditer(html):
        href = _HREF.search(m.group(1))
        if href is None:
            continue
        link = _html.unescape(href.group(1))
        if not anchor_ids.intersection(_iids(link)):
            continue
        name = normalize_text(_html.unescape(_TAG.sub(" ", m.group(2))))
        if not name or name.casefold() in kinds:
            continue
        seller_id = _profile_id(link)
        return classify(
            "avito",
            name,
            make_source("avito", "dom:offer", "seller-link/link"),
            {"seller_id": seller_id} if seller_id else None,
        )
    return SellerFound(None, SellerStatus.UNKNOWN_LAYOUT, None)


def _page_kinds(html: str, buyer: dict[str, Any] | None) -> frozenset[str]:
    """Типы продавца этой страницы: закрытый список плюс её собственная подпись.

    Множество одно на оба пути чтения имени. Иначе тип, отвергнутый в
    состоянии, проходил бы резервом по разметке: ссылка на профиль с нашим
    ``iid`` — якорь, и «Агентство» в ней ушло бы клиентом как имя продавца.
    """
    labels = [_dom_label(html)]
    if buyer is not None:
        labels.append(_text(_dict(_dict(buyer.get("seller")).get("labels")).get("nominative")))
    return SELLER_KINDS | {label.casefold() for label in labels if label}


def _dom_label(html: str) -> str | None:
    m = _SELLER_LABEL.search(html)
    if m is None:
        return None
    return normalize_text(_html.unescape(m.group(1))) or None


def _hides_name(buyer: dict[str, Any]) -> bool:
    profile = _dict(_dict(buyer.get("contactBarInfo")).get("publicProfileInfo"))
    return profile.get("hideSellerName") is True


def _result(name: str | None, seller: SellerFound) -> RungResult:
    if not name and seller.status not in _RESOLVED:
        return RungResult(verdict=Verdict.SCHEMA_DRIFT)
    complete = bool(name) and seller.status in _RESOLVED
    return RungResult(
        verdict=Verdict.OK if complete else Verdict.PARTIAL,
        name=name,
        seller_name=seller.name,
        seller_id=seller.seller_id,
        seller_status=seller.status,
        seller_source=seller.source,
    )


def _h1(html: str) -> str | None:
    m = _H1.search(html)
    if m is None:
        return None
    return normalize_text(_html.unescape(_TAG.sub(" ", m.group(1)))) or None


def _text(value: Any) -> str | None:
    """Строка из состояния. Сущности не разворачиваются: это JSON, не HTML."""
    if not isinstance(value, str):
        return None
    return normalize_text(value) or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iids(link: str) -> list[str]:
    return parse_qs(urlsplit(link).query).get("iid", [])


def _profile_id(link: str) -> str | None:
    m = _PROFILE_ID.match(urlsplit(link).path)
    return m.group(1) if m else None


__all__ = [
    "CHALLENGE_MARKERS",
    "CHALLENGE_STATUS",
    "MARKER_SCAN_CHARS",
    "ORIGIN",
    "ROUTE_NODE",
    "SELLER_KINDS",
    "classify_response",
    "hydration_state",
    "is_challenge",
    "parse_pdp",
    "route_node",
]
