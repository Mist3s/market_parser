"""Грамматика ``descr``: длина, двусторонний veto, guard чужих, безопасность удаления."""

from __future__ import annotations

import itertools
import re
from datetime import date
from pathlib import Path

import pytest

from mktlink.proxy6.descr import (
    BIRTH_DESCR,
    DESCR_LEN,
    MK_VALUES,
    NONCE_ALPHABET,
    NONCE_LEN,
    PROXY6_DESCR_MAX,
    Descr,
    DescrError,
    is_deletable,
    is_nonce,
    is_ours,
    pack_birth,
    pack_nonce,
    pack_write,
    unpack,
)

ROLES = ("a", "w", "r")
RENEWS = ("R", "X")
TIERS = ("s", "d")


def test_every_reachable_tag_is_exactly_19_chars() -> None:
    """Не выборочно, а по всему произведению значений."""
    seen = 0
    for mk, tier, role, renew, gen in itertools.product(
        sorted(MK_VALUES), TIERS, ROLES, RENEWS, (0, 7, 42, 99)
    ):
        cur = Descr(mk=mk, tier=tier, role="a", renew=renew, gen=gen)
        out = pack_write(cur, role=role, mk=mk)
        assert len(out) == DESCR_LEN, f"{out!r} is {len(out)}"
        assert len(out) <= PROXY6_DESCR_MAX
        assert unpack(out) == Descr(mk=mk, tier=tier, role=role, renew=renew, gen=gen)
        seen += 1
    assert seen == 5 * 2 * 3 * 2 * 4


def test_birth_tag_is_19_for_both_tiers() -> None:
    for version, tier in ((3, "s"), (4, "d")):
        out = pack_birth(version, 1)
        assert len(out) == DESCR_LEN
        d = unpack(out)
        assert (d.tier, d.mk, d.role, d.renew) == (tier, "xx", "w", "R")
        assert d.version == version


def test_nonce_is_21_chars() -> None:
    out = pack_nonce(date(2026, 9, 2), "7Q3XK9")
    assert out == "mp1.ord.260902.7Q3XK9"
    assert len(out) == NONCE_LEN <= PROXY6_DESCR_MAX
    assert is_nonce(out)
    assert is_ours(out)
    # Nonce не разбирается классовой грамматикой и не подлежит удалению.
    with pytest.raises(DescrError):
        unpack(out)
    assert not is_deletable(out)


def test_nonce_alphabet_excludes_confusable_glyphs() -> None:
    assert len(NONCE_ALPHABET) == len(set(NONCE_ALPHABET)) == 32
    for c in "01IO":
        assert c not in NONCE_ALPHABET
    with pytest.raises(ValueError):
        pack_nonce(date(2026, 9, 2), "0OI1XY")


def test_veto_is_two_sided_and_X_wins_forever() -> None:
    """Любой источник в одиночку запрещает продление, и обратного хода нет."""
    renewable = Descr(mk="mp", tier="s", role="a", renew="R", gen=1)

    # Локальная запись одна, живой descr говорит R -> получаем X.
    assert ".X." in pack_write(renewable, role="a", local_veto=True)
    # Живой descr говорит X, локальной записи нет -> всё равно X.
    burnt = Descr(mk="mp", tier="s", role="a", renew="X", gen=1)
    assert ".X." in pack_write(burnt, role="a", local_veto=False)
    # И даже при явном отсутствии обоих veto X не возвращается к R.
    assert ".X." in pack_write(burnt, role="w", local_veto=False)
    # Только чистый случай остаётся продлеваемым.
    assert ".R." in pack_write(renewable, role="a", local_veto=False)


def test_role_change_necessarily_rewrites_renew() -> None:
    """Позиционный тэг: «сменить роль, флаг оставить» одним setdescr невыразимо."""
    burnt = Descr(mk="oz", tier="s", role="a", renew="X", gen=2)
    out = pack_write(burnt, role="r")
    assert out == "mp1.oz.s.r.X.ru.g02"
    # Флаг переписан вместе с ролью — и он обязан был остаться X.
    assert unpack(out).renew == "X"


def test_namespace_guard_protects_human_bought_proxies() -> None:
    assert is_ours("mp1.mp.s.a.R.ru.g01")
    for foreign in ("my-proxy", "", "MP1.mp.s.a.R.ru.g01", "mp2.mp.s.a.R.ru.g01", "for-scraper"):
        assert not is_ours(foreign)
        with pytest.raises(DescrError):
            unpack(foreign)


def test_deletable_only_matches_retired_and_never_renew() -> None:
    assert is_deletable("mp1.oz.s.r.X.ru.g07")
    assert is_deletable("mp1.mp.d.r.X.ru.g00")
    # Активный — нет. Продлеваемый — нет. Ожидающий — нет.
    for keep in (
        "mp1.oz.s.a.X.ru.g07",  # ещё в обслуживании
        "mp1.oz.s.r.R.ru.g07",  # списан, но не помечен
        "mp1.oz.s.w.X.ru.g07",  # резерв
        "mp1.ord.260902.7Q3XK9",  # nonce
        "someone-elses",  # чужой
    ):
        assert not is_deletable(keep), keep


def test_gen_and_version_round_trip() -> None:
    d = unpack("mp1.ym.d.a.R.ru.g42")
    assert (d.mk, d.tier, d.role, d.renew, d.gen) == ("ym", "d", "a", "R", 42)
    assert d.version == 4
    assert d.gen_tag() == "g42"
    assert not d.never_renew and not d.retired

    s = unpack("mp1.wb.s.r.X.ru.g00")
    assert s.version == 3 and s.never_renew and s.retired


def test_malformed_tags_are_rejected_not_coerced() -> None:
    for bad in (
        "mp1.zz.s.a.R.ru.g01",  # неизвестный маркетплейс
        "mp1.mp.x.a.R.ru.g01",  # неизвестный тариф
        "mp1.mp.s.q.R.ru.g01",  # роль q удалена вместе с карантином
        "mp1.mp.s.a.Y.ru.g01",  # неизвестный флаг
        "mp1.mp.s.a.R.kz.g01",  # другая гео
        "mp1.mp.s.a.R.ru.g1",  # одна цифра генерации
        "mp1.mp.s.a.R.ru.g001",  # три
        "mp1.mp.s.a.R.ru.01",  # без префикса g
        " mp1.mp.s.a.R.ru.g01",  # ведущий пробел
    ):
        with pytest.raises(DescrError):
            unpack(bad)


def test_invalid_arguments_are_refused() -> None:
    cur = Descr(mk="mp", tier="s", role="a", renew="R", gen=1)
    with pytest.raises(ValueError):
        pack_write(cur, role="q")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        pack_write(cur, role="a", mk="detmir")
    with pytest.raises(ValueError):
        pack_birth(6, 1)  # IPv6 не в скоупе
    with pytest.raises(ValueError):
        pack_birth(3, 100)


def test_R_has_exactly_one_named_source_in_the_package() -> None:
    """CI-ассерт: единственный литерал с ``.R.`` — константа BIRTH_DESCR.

    Иначе «продлеваемость» просачивается литералами по коду, и однажды один из
    них перезапишет метку never-renew — ровно тот дефект, из-за которого
    источник вынесен в одну константу.
    """
    pkg = Path(__file__).resolve().parents[1] / "mktlink"
    assert pkg.is_dir()
    offenders: list[tuple[str, int, str]] = []
    for path in sorted(pkg.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.R\.", line) and "BIRTH_DESCR" not in line:
                # Прозаические упоминания в docstring'ах и комментариях не считаются
                # источником: они не попадают в setdescr.
                stripped = line.strip()
                if stripped.startswith(("#", "*", '"""', "'''")):
                    continue
                offenders.append((str(path.relative_to(pkg)), lineno, stripped))
    assert offenders == [], f"unnamed '.R.' literals: {offenders}"
    assert ".R." in BIRTH_DESCR
