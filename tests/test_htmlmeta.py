"""Чтение мета-разметки. Главный тест здесь — регрессия на порядок атрибутов.

Дефект, который эти тесты закрывают, стоил не потерянного поля, а неверного
диагноза по целому маркетплейсу и штрафа исправному адресу. Поэтому проверка
идёт не только на функцию, но и на её последствие в классификаторе.
"""

from __future__ import annotations

import pathlib

from mktlink.extract.htmlmeta import link_href, meta_content, title
from mktlink.marketplaces import ym
from mktlink.marketplaces.selectors import DEFAULTS
from mktlink.marketplaces.verdict import SellerStatus, Verdict

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: Порядок Ozon: сначала ``property``, потом ``content``.
PROPERTY_FIRST = '<meta property="og:url" content="https://example.com/a">'
#: Порядок Яндекса: сначала ``content``. ЗАМЕРЕНО на живой карточке.
CONTENT_FIRST = '<meta content="https://example.com/b" property="og:url">'


def test_reads_both_attribute_orders() -> None:
    """Оба порядка обязаны читаться. Это и есть суть модуля."""
    assert meta_content(PROPERTY_FIRST, "og:url") == "https://example.com/a"
    assert meta_content(CONTENT_FIRST, "og:url") == "https://example.com/b"


def test_content_first_order_is_what_broke_before() -> None:
    """Регрессия. Прежняя регулярка требовала property раньше content.

    Тест назван по причине, а не по функции: если он однажды упадёт, чинить
    надо не форму выражения, а понимание того, что порядок атрибутов в чужом
    HTML нам не принадлежит.
    """
    import re

    old = re.compile(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)', re.I)
    assert old.search(PROPERTY_FIRST) is not None
    assert old.search(CONTENT_FIRST) is None, "старая форма не могла совпасть — в этом и был баг"
    assert meta_content(CONTENT_FIRST, "og:url") is not None


def test_reads_name_as_well_as_property() -> None:
    """Twitter Cards пользуется ``name``, Open Graph — ``property``."""
    assert meta_content('<meta name="twitter:card" content="summary">', "twitter:card") == "summary"


def test_key_match_is_case_insensitive_but_exact() -> None:
    assert meta_content('<meta property="OG:URL" content="x">', "og:url") == "x"
    # Префикс не считается совпадением: og:url и og:url_extra — разные ключи.
    assert meta_content('<meta property="og:url_extra" content="x">', "og:url") is None


def test_first_occurrence_wins() -> None:
    html = '<meta property="og:title" content="one"><meta property="og:title" content="two">'
    assert meta_content(html, "og:title") == "one"


def test_empty_content_is_not_a_value() -> None:
    """Пустая строка — это отсутствие значения, а не значение.

    Иначе ``identity()`` вернул бы ``""``, которое истинно как «страница
    назвала себя» и ложно как URL, и проверка якоря молча провалилась бы.
    """
    assert meta_content('<meta property="og:url" content="">', "og:url") is None


def test_entities_are_unescaped() -> None:
    assert meta_content('<meta property="og:title" content="a &amp; b">', "og:title") == "a & b"


def test_link_rel_matches_by_token() -> None:
    """``rel`` может нести несколько значений через пробел."""
    assert link_href('<link rel="canonical" href="/a">', "canonical") == "/a"
    assert link_href('<link rel="alternate canonical" href="/b">', "canonical") == "/b"
    assert link_href('<link href="/c" rel="canonical">', "canonical") == "/c"
    # Подстрока не в счёт: canonicalize — не canonical.
    assert link_href('<link rel="canonicalize" href="/d">', "canonical") is None


def test_title_strips_tags_and_entities() -> None:
    assert title("<title>a &amp; <b>b</b></title>") == "a & b"
    assert title("<html></html>") is None


# --- последствие в классификаторе -------------------------------------------


def test_live_ym_card_is_not_classified_as_a_block() -> None:
    """Фикстура живой карточки Я.Маркета обязана классифицироваться как годная.

    До исправления она давала ``SILENT_EMPTY`` — вердикт из
    :data:`~mktlink.marketplaces.verdict.CHARGES_PROXY`, то есть исправный
    адрес получал улику против себя за корректно отданную страницу.
    """
    html = (FIXTURES / "ym_pdp.html").read_text(encoding="utf-8")
    verdict = ym.classify_response(html, anchor_ids={"101814267477"}, status=200)
    assert verdict is None, "годная карточка не должна давать вердикт вовсе"


def test_live_ym_card_yields_name_and_unverified_seller() -> None:
    html = (FIXTURES / "ym_pdp.html").read_text(encoding="utf-8")
    r = ym.parse_pdp(html, DEFAULTS["ym"], anchor_ids={"101814267477"}, status=200)

    assert r.verdict is Verdict.PARTIAL
    assert r.name is not None and "Да Хун Пао" in r.name
    # Сущности развёрнуты: сырых &#34; в ответе быть не может.
    assert "&#" not in r.name
    assert r.seller_name == "Чайный базар"
    # Продавец найден, но НЕ подтверждён как продавец запрошенного оффера.
    assert r.seller_status is SellerStatus.CARD_DEFAULT
    assert not r.complete, "card_default не считается полнотой — иначе мы врём"
