"""Авито: реестр, разбор живой карточки, снятие челленджа QRATOR и проводка.

Фикстуры сняты 2026-10-01 с карточки 7822637882: ``avito_pdp.html`` — страница
после рукопожатия (урезана, рекламные ветки с адресом клиента выброшены),
``avito_challenge.html`` — ответ ``439`` на первый GET без cookie.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import pathlib
import re
import time
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from mktlink.api.routes import Deps, handle
from mktlink.api.schemas import ProductRequest
from mktlink.api.wiring import DIRECT_EGRESS_ID, DIRECT_ONLY, WORKS_DIRECT, build_ladder
from mktlink.budget import LADDER, plan
from mktlink.constants import (
    BUDGET_FLOOR_MS,
    MIN_INTERVAL_MS,
    REDIRECT_HOPS_MAX,
    RETRY_AFTER_FETCH_S,
)
from mktlink.egress import unblock
from mktlink.egress.client import BodyTooLarge, EgressClient
from mktlink.extract.seller import is_anchored
from mktlink.marketplaces import avito
from mktlink.marketplaces.base import Context, run_ladder
from mktlink.marketplaces.lanes import LANES, AvitoLane, SimpleLease, build_lane
from mktlink.marketplaces.selectors import REGISTRY as SELECTORS
from mktlink.marketplaces.verdict import SellerStatus, Verdict
from mktlink.settings import Settings
from mktlink.store.cache import ProductCache
from mktlink.store.db import connect, init_db
from mktlink.timing.deadline import Deadline, DeadlineExceeded
from mktlink.urls.canonical import canonicalise
from mktlink.urls.registry import (
    NotAProductUrl,
    UnknownHost,
    match_path,
    rule_for_host,
    unwind_eligible,
)
from mktlink.urls.validate import validate

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PDP = (FIXTURES / "avito_pdp.html").read_text(encoding="utf-8")
CHALLENGE = (FIXTURES / "avito_challenge.html").read_text(encoding="utf-8")

ITEM = "7822637882"
URL = (
    "https://www.avito.ru/kaliningrad/produkty_pitaniya/"
    "da_hun_pao_press_lodochka_5_sht_50_g_7822637882"
)
NAME = "Да Хун Пао пресс «Лодочка» 5 шт (50 г)"
SELLER = "Zavarka39 - Китайский чай"
SELLER_ID = "496c10b485c0cc17027cc587d150d0d1"

_STATE = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("[^"\\]*(?:\\.[^"\\]*)*")\)'
)


def _encode(state: dict) -> str:
    """Литерал состояния в той же двойной кодировке, что у живой страницы."""
    inner = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(inner, ensure_ascii=False).replace("<", "\\u003c")


def _with_state(html: str, mutate) -> str:
    m = _STATE.search(html)
    assert m is not None
    state = json.loads(json.loads(m.group(1)))
    mutate(state)
    return html[: m.start(1)] + _encode(state) + html[m.end(1) :]


def _redirect_page(target: str) -> str:
    """Страница снятого объявления: 200, а в узле маршрута увод по ``target``."""
    state = {
        "loaderData": {
            "0": None,
            avito.ROUTE_NODE: {"type": "redirect", "redirectCode": 301, "redirectUrl": target},
        },
        "actionData": None,
        "errors": None,
    }
    return (
        "<html><body><script>window.__staticRouterHydrationData = JSON.parse("
        + _encode(state)
        + ");</script></body></html>"
    )


def _buyer(state: dict) -> dict:
    return state["loaderData"][avito.ROUTE_NODE]["buyerItem"]


def _canon(url: str):
    p = validate(url)
    return canonicalise(p.host, p.path, p.query, match_path(p.host, p.path))


_SELLER_SPAN = f'<span class="">{SELLER}</span>'
_LABEL_MARKUP = 'data-marker="seller-info/label">Частное лицо<'


def _with_markup(html: str, *, name: str | None = None, label: str | None = None) -> str:
    """Подменить имя в ссылке на профиль и подпись типа продавца в разметке."""
    if name is not None:
        assert _SELLER_SPAN in html
        html = html.replace(_SELLER_SPAN, f'<span class="">{name}</span>')
    if label is not None:
        assert _LABEL_MARKUP in html
        html = html.replace(_LABEL_MARKUP, f'data-marker="seller-info/label">{label}<')
    return html


def _label_everywhere_in_state(label: str, *, as_page_label: bool):
    """Мутатор: во всех трёх слотах имени — ``label``, а не имя."""

    def mutate(state: dict) -> None:
        bi = _buyer(state)
        if as_page_label:
            bi["seller"]["labels"]["nominative"] = label
        bi["contactBarInfo"]["seller"]["name"] = label
        bi["seller"]["name"] = label
        bi["contactBarInfo"]["publicProfileInfo"]["itemSellerName"] = label

    return mutate


# --- реестр и ключ кэша -------------------------------------------------------------


def test_example_link_is_a_product_with_exactly_one_anchor() -> None:
    """Одна именованная группа: все группы пути уходят в якоря разбора."""
    m = match_path("www.avito.ru", urlsplit(URL).path)
    assert m.is_pdp
    assert m.ids == {"item_id": ITEM}


@pytest.mark.parametrize(
    "url",
    [
        URL,
        URL + "/",
        URL + "?utm_source=telegram&context=H4sIAAAAAAAA_wE",
        URL.replace("www.avito.ru", "avito.ru"),
        URL.replace("www.avito.ru", "m.avito.ru"),
    ],
)
def test_host_slash_and_query_do_not_split_the_cache(url: str) -> None:
    c = _canon(url)
    assert c.url == URL
    assert c.cache_key == f"pl:v1:avito:i{ITEM}@*"
    assert c.offer == ()


def test_city_and_slug_do_not_split_the_cache_either() -> None:
    """Замерено: чужой город и слаг отдают ту же карточку. Ключ — номер."""
    other = "https://www.avito.ru/moskva/produkty_pitaniya/chay_7822637882"
    assert _canon(other).cache_key == _canon(URL).cache_key


def test_listing_is_the_offer_so_the_link_is_pinned() -> None:
    assert _canon(URL).pinned is True
    assert rule_for_host("www.avito.ru").listing_is_offer is True
    # Модельный URL Ozon закреплённым не становится.
    ozon = _canon("https://www.ozon.ru/product/chay-3160461596/")
    assert ozon.pinned is False
    assert _canon("https://www.ozon.ru/product/chay-3160461596/?sku=1").pinned is True


@pytest.mark.parametrize(
    ("path", "classified"),
    [
        ("/kaliningrad", True),
        ("/kaliningrad/produkty_pitaniya", True),
        ("/kaliningrad/produkty_pitaniya/chay-ASgBAgICAUSo5A2", True),
        ("/brands/496c10b485c0cc17027cc587d150d0d1", True),
        ("/user/abc/profile", True),
        # Форма /{номер} есть, но через раскрутку не проходит: см. реестр.
        ("/7822637882", False),
    ],
)
def test_not_a_listing(path: str, classified: bool) -> None:
    with pytest.raises(NotAProductUrl) as exc:
        match_path("www.avito.ru", path)
    assert (exc.value.classified is not None) is classified
    assert not unwind_eligible("www.avito.ru", path)


@pytest.mark.parametrize("host", ["avito.ru.evil.com", "www.avito.ru.example", "evilavito.ru"])
def test_lookalike_hosts_are_not_ours(host: str) -> None:
    with pytest.raises(UnknownHost):
        match_path(host, urlsplit(URL).path)


def test_global_budget_floor_affords_the_avito_rung() -> None:
    """Пол Авито не выше пола Я.Маркета, поэтому общий пол не сдвинулся."""
    assert LADDER["avito"][0].floor_ms <= LADDER["ym"][0].floor_ms
    p = plan(BUDGET_FLOOR_MS, "avito", REDIRECT_HOPS_MAX)
    assert p.caps == (LADDER["avito"][0].floor_ms,)


# --- разбор карточки ----------------------------------------------------------------


def test_live_card_gives_name_and_anchored_seller() -> None:
    r = avito.parse_pdp(PDP, anchor_ids={ITEM})
    assert r.verdict is Verdict.OK
    assert r.name == NAME
    assert r.seller_name == SELLER
    assert r.seller_id == SELLER_ID
    assert r.seller_status is SellerStatus.RESOLVED
    assert r.seller_source == "avito:state:buyerItem.contactBarInfo.seller.name"
    assert is_anchored(r.seller_source)


def test_seller_type_is_never_taken_for_a_name() -> None:
    """Под ``sellerName`` лежит тип продавца — ловушка для обхода по ключам.

    В живой карточке там «Частное лицо», но с ним тест прошёл бы и при чтении
    поля: его отсёк бы закрытый список. Поэтому здесь тип вне списка и не
    равный подписи страницы — отсечь его, если бы поле читалось, было бы нечем.
    """

    def drop_names(state: dict) -> None:
        bi = _buyer(state)
        del bi["contactBarInfo"]["seller"]["name"]
        del bi["seller"]["name"]
        profile = bi["contactBarInfo"]["publicProfileInfo"]
        del profile["itemSellerName"]
        assert profile["sellerName"] == "Частное лицо"
        profile["sellerName"] = "Агентство"

    html = _with_state(PDP, drop_names)
    # И разметку продавца убираем, чтобы резерв не подставил имя за состояние.
    html = html.replace('data-marker="seller-link/link"', 'data-marker="gone"')
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.PARTIAL
    assert r.name == NAME
    assert r.seller_name is None
    assert r.seller_status is SellerStatus.UNKNOWN_LAYOUT
    assert "Агентство" not in repr(r)


def test_label_of_this_page_counts_as_a_seller_type() -> None:
    def relabel(state: dict) -> None:
        bi = _buyer(state)
        bi["seller"]["labels"]["nominative"] = "Агентство"
        bi["contactBarInfo"]["seller"]["name"] = "Агентство"

    r = avito.parse_pdp(_with_state(PDP, relabel), anchor_ids={ITEM})
    assert r.seller_name == SELLER
    assert r.seller_source == "avito:state:buyerItem.seller.name"


@pytest.mark.parametrize("source", ["state", "markup"])
def test_page_label_rejects_the_name_on_both_paths(source: str) -> None:
    """Подпись типа, откуда бы она ни пришла, отсекает имя и в состоянии, и в разметке.

    Иначе тип, отвергнутый в одном месте, проходил бы через другое: ссылка на
    профиль с нашим ``iid`` — якорь, и «Агентство» ушло бы клиенту как имя.
    """
    label = "Агентство"
    html = _with_state(PDP, _label_everywhere_in_state(label, as_page_label=source == "state"))
    html = _with_markup(html, name=label, label=label if source == "markup" else None)
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.PARTIAL
    assert r.name == NAME
    assert r.seller_name is None
    assert label not in repr(r)


def test_markup_label_rejects_the_name_without_state() -> None:
    html = PDP.replace("__staticRouterHydrationData", "__renamedState")
    html = _with_markup(html, name="Застройщик", label="Застройщик")
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.PARTIAL
    assert (r.name, r.seller_name) == (NAME, None)


def test_real_name_in_markup_survives_a_state_that_carries_only_the_label() -> None:
    """Обратная сторона: отсекается тип, а не всё, что лежит с ним рядом."""
    html = _with_state(PDP, _label_everywhere_in_state("Агентство", as_page_label=True))
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.OK
    assert (r.seller_name, r.seller_id) == (SELLER, SELLER_ID)
    assert r.seller_source == "avito:dom:offer:seller-link/link"


def test_seller_of_another_listing_is_refused() -> None:
    """Продавец принимается, только если его ссылка или бар несут наш номер."""

    def move(state: dict) -> None:
        _buyer(state)["contactBarInfo"]["itemId"] = 1111111111

    html = _with_state(PDP, move).replace(f"iid={ITEM}", "iid=1111111111")
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.PARTIAL
    assert r.name == NAME
    assert r.seller_name is None


def test_card_of_another_listing_is_drift_not_data() -> None:
    r = avito.parse_pdp(PDP, anchor_ids={"1234567890"})
    assert r.verdict is Verdict.SCHEMA_DRIFT
    assert r.name is None


def test_hidden_seller_name_is_not_returned_from_anywhere() -> None:
    def hide(state: dict) -> None:
        _buyer(state)["contactBarInfo"]["publicProfileInfo"]["hideSellerName"] = True

    r = avito.parse_pdp(_with_state(PDP, hide), anchor_ids={ITEM})
    assert r.name == NAME
    assert r.seller_name is None
    assert SELLER not in repr(r)


def test_markup_alone_is_enough() -> None:
    """Состояние переименовали — работает резерв по разметке."""
    html = PDP.replace("__staticRouterHydrationData", "__renamedState")
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.verdict is Verdict.OK
    assert (r.name, r.seller_name, r.seller_id) == (NAME, SELLER, SELLER_ID)
    assert r.seller_source == "avito:dom:offer:seller-link/link"


def test_decoy_seller_link_of_another_listing_is_skipped() -> None:
    decoy = (
        '<a data-marker="seller-link/link" href="/brands/deadbeef?iid=1111111111">'
        "<span>Чужой продавец</span></a>"
    )
    html = PDP.replace("__staticRouterHydrationData", "__renamedState")
    html = html.replace("<body>", "<body>" + decoy, 1)
    assert html.index(decoy) < html.index(SELLER)
    r = avito.parse_pdp(html, anchor_ids={ITEM})
    assert r.seller_name == SELLER


def test_markup_of_another_listing_is_drift() -> None:
    html = PDP.replace("__staticRouterHydrationData", "__renamedState")
    r = avito.parse_pdp(html, anchor_ids={"1234567890"})
    assert r.verdict is Verdict.SCHEMA_DRIFT


def test_removed_listing_is_a_positive_not_found() -> None:
    """Замерено: несуществующий номер — 200 и увод в категорию в состоянии."""
    page = _redirect_page("/kaliningrad/produkty_pitaniya")
    assert avito.parse_pdp(page, anchor_ids={ITEM}).verdict is Verdict.NOT_FOUND


def test_redirect_to_the_same_listing_is_not_absence() -> None:
    page = _redirect_page(f"/moskva/produkty_pitaniya/chay_{ITEM}")
    assert avito.parse_pdp(page, anchor_ids={ITEM}).verdict is Verdict.SCHEMA_DRIFT


def test_challenge_is_recognised_by_status_and_by_body() -> None:
    assert avito.parse_pdp(CHALLENGE, anchor_ids={ITEM}, status=439).verdict is Verdict.CAPTCHA
    assert avito.parse_pdp(CHALLENGE, anchor_ids={ITEM}, status=200).verdict is Verdict.CAPTCHA
    # Карточка маркеров челленджа не несёт.
    assert not avito.is_challenge(200, PDP)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (403, Verdict.CAPTCHA),
        (429, Verdict.HTTP_429),
        (502, Verdict.UPSTREAM_ERROR),
        (301, Verdict.SCHEMA_DRIFT),
        (404, Verdict.NOT_FOUND),
        (410, Verdict.NOT_FOUND),
    ],
)
def test_status_comes_first(status: int, verdict: Verdict) -> None:
    assert avito.parse_pdp(PDP, anchor_ids={ITEM}, status=status).verdict is verdict


def test_blank_page_is_silence() -> None:
    assert avito.parse_pdp("  ", anchor_ids={ITEM}).verdict is Verdict.SILENT_EMPTY
    assert avito.parse_pdp("<html></html>", anchor_ids={ITEM}).verdict is Verdict.SILENT_EMPTY


_V4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)")
#: Не меньше трёх двоеточий: «a::before» из CSS — тоже валидный IPv6.
_V6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){3,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")


def _addresses(text: str) -> list[str]:
    found = _V4.findall(text)
    for cand in _V6.findall(text):
        try:
            ipaddress.ip_address(cand)
        except ValueError:
            continue
        found.append(cand)
    return found


def test_fixture_carries_no_client_address() -> None:
    """В ``rmp`` живой страницы лежал IP клиента; в фикстуре его быть не должно."""
    # Сперва — что проверка вообще ловит: адрес в конце фразы и сжатый IPv6.
    probe = '"ip":"203.0.113.7". 2001:db8:85a3::8a2e:370:7334'
    assert _addresses(probe) == ["203.0.113.7", "2001:db8:85a3::8a2e:370:7334"]
    assert not _addresses("1.2.3.4.5 a::before 12:30:00")
    assert not _addresses(PDP)
    assert not _addresses(CHALLENGE)


# --- снятие челленджа ------------------------------------------------------------------


def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(claims)}.c2ln"


class FakeAvito:
    """Авито в миниатюре: те же шаги рукопожатия, что в замере 2026-10-01."""

    def __init__(
        self,
        page: str = PDP,
        *,
        compl: int = 3,
        verified: bool = True,
        unblock_ttl: int | None = 420,
        set_challenge: bool = True,
        still_blocked: bool = False,
        page_failures: int = 0,
        pow_status: dict[str, int] | None = None,
    ) -> None:
        self.page = page
        self.compl = compl
        self.verified = verified
        self.unblock_ttl = unblock_ttl
        self.set_challenge = set_challenge
        self.still_blocked = still_blocked
        #: Сколько GET, которые отдали бы карточку, рвутся на транспорте.
        self.page_failures = page_failures
        #: Статус эндпоинта рукопожатия вместо его ответа: путь → статус.
        self.pow_status = pow_status or {}
        self.valid: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.sessions: list[FakeSession] = []
        self._n = 0

    def session(self, proxy_url: str | None) -> FakeSession:
        s = FakeSession(self, proxy_url)
        self.sessions.append(s)
        return s

    def next_challenge(self) -> str:
        self._n += 1
        return f"ch{self._n}"


class FakeSession:
    def __init__(self, server: FakeAvito, proxy_url: str | None) -> None:
        self.server = server
        self.proxy_url = proxy_url
        self.jar: dict[tuple[str, str], str] = {}
        self.posts: list[tuple[str, dict, dict[str, str]]] = []
        self.closed = False

    async def get(self, url, *, headers, timeout_ms):
        assert timeout_ms > 0
        srv = self.server
        srv.calls.append(("GET", url))
        if self.cookie("u") in srv.valid and not srv.still_blocked:
            if srv.page_failures:
                srv.page_failures -= 1
                raise unblock.TransportError("connection reset")
            return 200, srv.page
        if srv.set_challenge:
            self.jar[("pow_challenge", "www.avito.ru")] = srv.next_challenge()
        return unblock.CHALLENGE_STATUS, CHALLENGE

    async def post_json(self, url, payload, *, headers, timeout_ms):
        assert timeout_ms > 0
        srv = self.server
        path = urlsplit(url).path
        srv.calls.append(("POST", path))
        self.posts.append((path, payload, headers))
        if path in srv.pow_status:
            return srv.pow_status[path], ""
        if path == unblock.GET_PATH:
            if payload.get("challenge") != self.cookie("pow_challenge"):
                return 400, '{"error":"bad challenge"}'
            cid = f"id-{payload['challenge']}"
            claims = {"compl": srv.compl, "id": cid, "iss": "firewall-captcha", "v": 1}
            self.jar[("u", ".avito.ru")] = f"u-{cid}"
            self.jar[("v", ".avito.ru")] = "v1"
            body = {"success": {"result": {"challenge_jwt": _jwt(claims)}}}
            return 200, json.dumps(body)
        if path == unblock.VERIFY_PATH:
            claims = unblock.jwt_payload(payload["challenge"])
            digest = hashlib.sha256(f"{claims['id']}:{payload['nonce']}".encode()).hexdigest()
            ok = srv.verified and digest.startswith("0" * claims["compl"])
            if ok:
                srv.valid.add(self.cookie("u"))
            result: dict = {"verified": ok}
            if srv.unblock_ttl is not None:
                result["unblock_ttl"] = srv.unblock_ttl
            return 200, json.dumps({"success": {"result": result, "status": "ok"}})
        return 404, ""

    def cookie(self, name):
        return next((v for (n, _d), v in self.jar.items() if n == name), None)

    def cookies(self):
        return tuple((n, v, d) for (n, d), v in self.jar.items())

    def set_cookies(self, cookies):
        for n, v, d in cookies:
            self.jar[(n, d)] = v

    async def aclose(self):
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _fetch(u: unblock.Unblocker, *, max_bytes: int = 2 * 1024 * 1024):
    return await u.fetch(
        Deadline.start(10_000, "t"),
        URL,
        egress_id=DIRECT_EGRESS_ID,
        proxy_url=None,
        cap_ms=6000,
        reserve_ms=440,
        max_bytes=max_bytes,
    )


def test_challenge_status_is_one_number_in_both_layers() -> None:
    assert unblock.CHALLENGE_STATUS == avito.CHALLENGE_STATUS == 439


def test_jwt_payload_reads_claims_and_refuses_garbage() -> None:
    claims = {"compl": 3, "id": "abc", "unblock_ttl_sec": 420}
    assert unblock.jwt_payload(_jwt(claims)) == claims
    for bad in ("", "a.b", "a.!!!.c", f"a.{_b64(['x'])}.c"):
        with pytest.raises(unblock.PowFailed) as exc:
            unblock.jwt_payload(bad)
        assert exc.value.reason == "bad_jwt"


async def test_solve_finds_the_smallest_nonce() -> None:
    nonce = await unblock.solve("abc", 3, Deadline.start(5000, "t"), 0)
    digest = hashlib.sha256(f"abc:{nonce}".encode()).hexdigest()
    assert digest.startswith("000")
    assert not any(
        hashlib.sha256(f"abc:{n}".encode()).hexdigest().startswith("000") for n in range(nonce)
    )


async def test_solve_refuses_work_it_cannot_finish() -> None:
    with pytest.raises(unblock.PowFailed) as exc:
        await unblock.solve("abc", unblock.MAX_COMPLEXITY + 1, Deadline.start(5000, "t"), 0)
    assert exc.value.reason == "too_complex"


async def test_solve_stops_at_the_deadline() -> None:
    # У этого id в первой порции решения с пятью нулями нет: проверено перебором.
    with pytest.raises(DeadlineExceeded):
        await unblock.solve("deadline-0", 5, Deadline.start(0, "t"), 0)


async def test_cold_fetch_solves_the_challenge_and_keeps_the_unblock() -> None:
    srv = FakeAvito()
    u = unblock.Unblocker(srv.session)
    status, body = await _fetch(u)
    assert status == 200 and body == PDP
    assert srv.calls == [
        ("GET", URL),
        ("POST", unblock.GET_PATH),
        ("POST", unblock.VERIFY_PATH),
        ("GET", URL),
    ]
    kept = u.get(DIRECT_EGRESS_ID)
    assert kept is not None
    names = {name for name, _value, _domain in kept.cookies}
    assert "u" in names and unblock.CHALLENGE_COOKIE not in names
    assert all(s.closed for s in srv.sessions)


async def test_handshake_posts_like_the_page_script() -> None:
    srv = FakeAvito()
    await _fetch(unblock.Unblocker(srv.session))
    (get_path, get_payload, headers), (verify_path, verify_payload, _) = srv.sessions[0].posts
    assert (get_path, verify_path) == (unblock.GET_PATH, unblock.VERIFY_PATH)
    assert get_payload == {"challenge": "ch1"}
    assert set(verify_payload) == {"challenge", "nonce"}
    assert isinstance(verify_payload["nonce"], int)
    assert headers["Content-Type"] == "application/json"
    assert headers["Origin"] == unblock.ORIGIN
    assert headers["Referer"] == URL


async def test_warm_fetch_presents_cookies_and_skips_the_handshake() -> None:
    srv = FakeAvito()
    u = unblock.Unblocker(srv.session)
    await _fetch(u)
    srv.calls.clear()
    status, _ = await _fetch(u)
    assert status == 200
    assert srv.calls == [("GET", URL)]
    assert len(srv.sessions) == 2, "сессия на вызов, cookie переносятся из кэша"


async def test_unblock_is_trusted_only_until_its_margin() -> None:
    srv = FakeAvito(unblock_ttl=420)
    clock = Clock()
    u = unblock.Unblocker(srv.session, clock=clock)
    await _fetch(u)
    clock.now += 420 - unblock.TTL_MARGIN_S - 1
    assert u.get(DIRECT_EGRESS_ID) is not None
    clock.now += 2
    assert u.get(DIRECT_EGRESS_ID) is None


async def test_ttl_falls_back_to_the_measured_default() -> None:
    srv = FakeAvito(unblock_ttl=None)
    clock = Clock()
    u = unblock.Unblocker(srv.session, clock=clock)
    await _fetch(u)
    kept = u.get(DIRECT_EGRESS_ID)
    assert kept is not None
    assert kept.expires_at == clock.now + unblock.DEFAULT_UNBLOCK_TTL_S - unblock.TTL_MARGIN_S


async def test_revoked_unblock_is_dropped_and_redone() -> None:
    srv = FakeAvito()
    u = unblock.Unblocker(srv.session)
    await _fetch(u)
    first = u.get(DIRECT_EGRESS_ID)
    srv.valid.clear()  # сервер отозвал разблокировку раньше срока
    srv.calls.clear()
    status, _ = await _fetch(u)
    assert status == 200
    assert [c[0] for c in srv.calls] == ["GET", "POST", "POST", "GET"]
    assert u.get(DIRECT_EGRESS_ID) != first


async def test_unverified_handshake_keeps_nothing() -> None:
    srv = FakeAvito(verified=False)
    u = unblock.Unblocker(srv.session)
    with pytest.raises(unblock.PowFailed) as exc:
        await _fetch(u)
    assert exc.value.reason == "not_verified"
    assert u.get(DIRECT_EGRESS_ID) is None
    assert all(s.closed for s in srv.sessions)


async def test_challenge_without_its_cookie_is_reported() -> None:
    srv = FakeAvito(set_challenge=False)
    with pytest.raises(unblock.PowFailed) as exc:
        await _fetch(unblock.Unblocker(srv.session))
    assert exc.value.reason == "no_challenge"


async def test_complexity_above_the_ceiling_is_not_attempted() -> None:
    srv = FakeAvito(compl=unblock.MAX_COMPLEXITY + 1)
    with pytest.raises(unblock.PowFailed) as exc:
        await _fetch(unblock.Unblocker(srv.session))
    assert exc.value.reason == "too_complex"
    assert ("POST", unblock.VERIFY_PATH) not in srv.calls


async def test_page_still_challenged_after_verify_is_not_kept() -> None:
    srv = FakeAvito(still_blocked=True)
    u = unblock.Unblocker(srv.session)
    status, _ = await _fetch(u)
    assert status == unblock.CHALLENGE_STATUS
    assert u.get(DIRECT_EGRESS_ID) is None


async def test_unblock_survives_a_page_fetch_that_broke() -> None:
    """Проверка пройдена, и cookie действуют, даже если страницу не довезли.

    Иначе сброс на последнем GET выбрасывал бы решённый челлендж, и повтор
    снова начинался бы с холодного рукопожатия.
    """
    srv = FakeAvito(page_failures=1)
    u = unblock.Unblocker(srv.session)
    with pytest.raises(unblock.TransportError):
        await _fetch(u)
    assert u.get(DIRECT_EGRESS_ID) is not None
    srv.calls.clear()
    assert await _fetch(u) == (200, PDP)
    assert srv.calls == [("GET", URL)], "повтор без рукопожатия"


@pytest.mark.parametrize("path", [unblock.GET_PATH, unblock.VERIFY_PATH])
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_handshake_endpoint_outage_is_not_a_refusal(path: str, status: int) -> None:
    srv = FakeAvito(pow_status={path: status})
    u = unblock.Unblocker(srv.session)
    with pytest.raises(unblock.UpstreamStatus) as exc:
        await _fetch(u)
    assert exc.value.status == status
    assert u.get(DIRECT_EGRESS_ID) is None


async def test_handshake_endpoint_refusal_is_still_a_failed_check() -> None:
    srv = FakeAvito(pow_status={unblock.GET_PATH: 400})
    with pytest.raises(unblock.PowFailed) as exc:
        await _fetch(unblock.Unblocker(srv.session))
    assert exc.value.reason == "get_failed"


async def test_body_over_the_ceiling_is_refused() -> None:
    srv = FakeAvito()
    with pytest.raises(BodyTooLarge):
        await _fetch(unblock.Unblocker(srv.session), max_bytes=1024)


# --- лейн ------------------------------------------------------------------------------


def _ctx() -> Context:
    return Context(
        marketplace="avito",
        canonical_url=URL,
        ids={"item_id": ITEM},
        anchor_ids=frozenset({ITEM}),
    )


async def _run_lane(unblocker: unblock.Unblocker):
    lane = build_lane(
        "avito",
        EgressClient(None),
        SELECTORS,
        SimpleLease(proxy_id=DIRECT_EGRESS_ID),
        unblocker=unblocker,
    )
    assert isinstance(lane, AvitoLane)
    return await run_ladder(Deadline.start(30_000, "t"), lane, _ctx(), 30_000, hops=0)


def test_lane_is_registered() -> None:
    assert LANES["avito"] is AvitoLane


async def test_lane_returns_name_and_seller() -> None:
    res, rung = await _run_lane(unblock.Unblocker(FakeAvito().session))
    assert rung == "avito.pdp_html"
    assert res.verdict is Verdict.OK
    assert (res.name, res.seller_name, res.seller_id) == (NAME, SELLER, SELLER_ID)


async def test_failed_handshake_is_a_challenge_with_its_reason() -> None:
    res, _ = await _run_lane(unblock.Unblocker(FakeAvito(verified=False).session))
    assert res.verdict is Verdict.CAPTCHA
    assert res.raw == {"pow": "not_verified"}


async def test_transport_failure_is_an_upstream_error() -> None:
    class Broken(FakeSession):
        async def get(self, url, *, headers, timeout_ms):
            raise unblock.TransportError("connection reset")

    srv = FakeAvito()
    res, _ = await _run_lane(unblock.Unblocker(lambda proxy: Broken(srv, proxy)))
    assert res.verdict is Verdict.UPSTREAM_ERROR


@pytest.mark.parametrize(
    ("status", "verdict"),
    [(503, Verdict.UPSTREAM_ERROR), (429, Verdict.HTTP_429), (400, Verdict.CAPTCHA)],
)
async def test_handshake_status_gets_the_verdict_of_the_same_page_status(
    status: int, verdict: Verdict
) -> None:
    srv = FakeAvito(pow_status={unblock.VERIFY_PATH: status})
    res, _ = await _run_lane(unblock.Unblocker(srv.session))
    assert res.verdict is verdict


# --- проводка ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    """База с живым прокси в пуле: Авито обязан его не взять."""
    init_db(tmp_path / "a.sqlite")
    c = connect(tmp_path / "a.sqlite")
    c.execute(
        "INSERT INTO proxy (p6_id, ip, host, port, user, pass, version, descr, state, term_end)"
        " VALUES (7, '1.2.3.4', 'h', 8000, 'u', 'p', 3, 'mp1.mp.s.a.R.ru.g01', 'active',"
        " unixepoch() + 864000)"
    )
    yield c
    c.close()


class RecordingCache(ProductCache):
    def __init__(self, conn) -> None:
        super().__init__(conn)
        self.ttls: list[int | None] = []

    def get(self, key, *, fresh_ttl_s=None):
        self.ttls.append(fresh_ttl_s)
        return super().get(key, fresh_ttl_s=fresh_ttl_s)


def _deps(conn, srv: FakeAvito, cache: ProductCache | None = None) -> Deps:
    async def no_client(url, **kw):
        raise AssertionError("клиент лейна в Авито не участвует")

    return Deps(
        cache=cache or ProductCache(conn),
        ladder=build_ladder(
            conn, EgressClient(no_client), unblocker=unblock.Unblocker(srv.session)
        ),
        product_ttl_s=111,
        product_ttl_pinned_s=222,
    )


def test_avito_is_direct_only() -> None:
    assert "avito" in DIRECT_ONLY
    assert "avito" not in WORKS_DIRECT, "это фолбэк при пустом пуле, а не запрет пула"


def test_avito_cannot_be_routed_through_the_scraping_api(monkeypatch) -> None:
    """Иначе каждый запрос падал бы KeyError на лестнице API, то есть 500."""
    # ``_env_file=None`` отключает только файл: окружение читается всё равно.
    monkeypatch.delenv("MKTLINK_SCRAPEDO_MARKETPLACES", raising=False)
    assert Settings(_env_file=None).scrapedo_marketplaces == ("ozon", "ym")
    with pytest.raises(ValidationError, match="avito"):
        Settings(_env_file=None, scrapedo_marketplaces=("ozon", "ym", "avito"))


async def test_avito_answers_without_the_proxy_pool(conn) -> None:
    srv = FakeAvito()
    code, body = await handle(ProductRequest(url=URL), _deps(conn, srv))
    assert code == 200, body.meta.reason
    assert body.marketplace == "avito"
    assert body.url.canonical == URL
    assert (body.product.id, body.product.id_kind, body.product.name) == (ITEM, "item_id", NAME)
    assert (body.seller.name, body.seller.id, body.seller.status) == (SELLER, SELLER_ID, "resolved")
    assert body.seller.kind == "third_party"
    assert (body.offer.selection, body.offer.stable, body.offer.ref) == ("explicit", True, None)
    assert body.meta.rung == "avito.pdp_html"

    # Пул не спрашивали: прокси 7 активен, но сессия открыта без него.
    assert [s.proxy_url for s in srv.sessions] == [None]
    row = conn.execute("SELECT p6_id, mp, verdict, egress FROM proxy_attempt").fetchone()
    assert (row["p6_id"], row["mp"], row["verdict"], row["egress"]) == (
        DIRECT_EGRESS_ID,
        "avito",
        "ok",
        "direct",
    )
    # CHECK в proxy_health и jar Авито не принимает — туда ничего не пишется.
    assert conn.execute("SELECT count(*) n FROM proxy_health").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) n FROM jar").fetchone()["n"] == 0
    slot = conn.execute("SELECT proxy_id FROM spacing WHERE mp = 'avito'").fetchone()
    assert slot["proxy_id"] == DIRECT_EGRESS_ID


async def test_avito_answer_is_cached_with_the_pinned_ttl(conn) -> None:
    srv = FakeAvito()
    cache = RecordingCache(conn)
    deps = _deps(conn, srv, cache)
    await handle(ProductRequest(url=URL), deps)
    srv.calls.clear()
    code, body = await handle(ProductRequest(url=URL + "?utm_source=x"), deps)
    assert code == 200 and body.meta.cache == "hit"
    assert srv.calls == [], "второй запрос в Авито не ходил"
    assert cache.ttls == [222, 222]


async def test_back_to_back_avito_misses_wait_their_turn(conn, monkeypatch) -> None:
    """Адрес у Авито один на все объявления, и слот спейсинга тоже один.

    Второй промах обязан дождаться слота и получить карточку, а не 202.
    """
    monkeypatch.setitem(MIN_INTERVAL_MS, "avito", 300)
    deps = _deps(conn, FakeAvito())
    await handle(ProductRequest(url=URL), deps)
    code, body = await handle(ProductRequest(url=URL, force_refresh=True), deps)
    assert (code, body.status, body.meta.cache) == (200, "ok", "miss")
    assert [stage for stage, _ms in body.meta.ledger] == ["spacing", "avito.pdp_html"]


async def test_avito_challenge_is_reported_as_a_challenge(conn) -> None:
    srv = FakeAvito(verified=False)
    code, body = await handle(ProductRequest(url=URL), _deps(conn, srv))
    assert code == 202
    assert body.meta.reason == "marketplace_challenge"
    row = conn.execute("SELECT verdict, egress FROM proxy_attempt").fetchone()
    assert (row["verdict"], row["egress"]) == ("captcha", "direct")
    assert conn.execute("SELECT count(*) n FROM proxy_health").fetchone()["n"] == 0


async def test_challenge_backend_outage_is_not_reported_as_a_block(conn) -> None:
    """Пятисотка ``firewallPow`` — сбой с ретраем через 3 с, а не блок на 25 с."""
    srv = FakeAvito(pow_status={unblock.GET_PATH: 503})
    code, body = await handle(ProductRequest(url=URL), _deps(conn, srv))
    assert code == 202
    assert body.meta.reason == "marketplace_error"
    assert body.meta.retry_after_seconds == RETRY_AFTER_FETCH_S


async def test_wait_that_leaves_no_room_for_the_rung_is_refused_at_once(conn) -> None:
    """Регрессия: допуск ожидания считался от хвоста стадии, без пола ступени.

    Долг 4 с на бюджете 5 с проходил: запрос спал четыре секунды, и уже потом
    лестница отказывала без единого вызова. Теперь отказ сразу и без брони.
    """
    due = time.time_ns() // 1_000_000 + 4000
    conn.execute(
        "INSERT INTO spacing (mp, proxy_id, next_allowed_ms) VALUES ('avito', ?, ?)",
        (DIRECT_EGRESS_ID, due),
    )
    srv = FakeAvito()
    started = time.monotonic()
    code, body = await handle(ProductRequest(url=URL, max_wait_ms=5000), _deps(conn, srv))
    assert time.monotonic() - started < 1, "отказ сразу, а не после сна"
    assert (code, body.meta.reason) == (202, "spacing_wait_exceeds_budget")
    assert srv.calls == []
    assert conn.execute("SELECT count(*) n FROM proxy_attempt").fetchone()["n"] == 0
    row = conn.execute("SELECT next_allowed_ms FROM spacing WHERE mp = 'avito'").fetchone()
    assert row["next_allowed_ms"] == due, "отказ слот не занял"


async def test_removed_avito_listing_is_not_found_unconfirmed(conn) -> None:
    srv = FakeAvito(_redirect_page("/kaliningrad/produkty_pitaniya"))
    code, body = await handle(ProductRequest(url=URL), _deps(conn, srv))
    assert code == 200
    assert body.status == "not_found_unconfirmed"
