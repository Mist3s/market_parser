"""Согласованность отпечатка: одно семейство, пинится только мажор."""

from __future__ import annotations

import pytest

from mktlink.egress.fingerprint import (
    FIREFOX,
    MAX_MAJOR_GAP,
    PROFILES,
    FingerprintDrift,
    FingerprintProfile,
    check_mint,
    firefox_major,
    pick_target,
    replay_headers,
)

#: ЗАМЕР: установленный Camoufox v152.0.4-beta.29 отдаёт именно это.
FF_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"


def test_camoufox_is_firefox_so_impersonate_must_be_firefox() -> None:
    assert FIREFOX.minter == "camoufox"
    assert FIREFOX.impersonate.startswith("firefox")
    assert FIREFOX.sends_sec_ch_ua is False, "Firefox их не шлёт — иначе это тэлл"


def test_all_three_marketplaces_share_one_family() -> None:
    assert set(PROFILES) == {"ozon", "wb", "ym"}
    assert len({p.impersonate for p in PROFILES.values()}) == 1


def test_a_profile_whose_halves_disagree_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        FingerprintProfile("bad", "camoufox", "firefox147", 128, "ru", False)
    with pytest.raises(ValueError):
        FingerprintProfile("chrome-ua-on-firefox", "camoufox", "firefox147", 147, "ru", True)


def test_mint_check_accepts_a_gap_but_not_a_forged_future() -> None:
    """Равенства мажоров не бывает — проверяется совместимость.

    Camoufox 152 против цели curl_cffi 147: разрыв структурный, у двух
    инструментов независимые циклы релизов. Требование равенства
    заблокировало бы минтинг целиком.
    """
    check_mint(FF_UA, FIREFOX)  # 152 против 147 — разрыв 5, в пределах допуска

    # Браузер СТАРШЕ подделываемой версии — запрещено.
    with pytest.raises(FingerprintDrift, match="does not exist yet"):
        check_mint(FF_UA.replace("152", "140"), FIREFOX)

    # Разрыв больше допуска — тоже отказ, и сообщение говорит, что делать.
    with pytest.raises(FingerprintDrift) as ei:
        check_mint(FF_UA.replace("152", "199"), FIREFOX)
    assert "MAX_MAJOR_GAP" in str(ei.value)


def test_the_target_is_picked_and_never_newer_than_the_browser() -> None:
    assert pick_target(152) == 147, "самая новая доступная, не новее браузера"
    assert pick_target(147) == 147
    assert pick_target(140) == 135
    assert pick_target(133) == 133
    with pytest.raises(FingerprintDrift):
        pick_target(100)


def test_the_gap_tolerance_is_a_number_in_code_not_a_hope() -> None:
    assert MAX_MAJOR_GAP == 8
    assert 152 - FIREFOX.firefox_major <= MAX_MAJOR_GAP, "замеренный разрыв укладывается"


def test_a_chrome_user_agent_from_the_minter_is_drift() -> None:
    chrome = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    with pytest.raises(FingerprintDrift):
        check_mint(chrome, FIREFOX)


def test_replay_uses_the_observed_ua_not_a_frozen_constant() -> None:
    """Camoufox ротирует отпечаток на каждый запуск, включая OS-токен."""
    rotated = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0"
    h = replay_headers(FIREFOX, rotated)
    assert h["User-Agent"] == rotated
    assert "sec-ch-ua" not in h
    assert h["Accept-Language"] == "ru-RU,ru;q=0.9"


def test_os_rotation_does_not_break_minting() -> None:
    """Camoufox ротирует OS-токен на каждый запуск; замер это подтвердил.

    В одном прогоне он отдал Windows NT 10.0, хотя машина — Linux.
    """
    for ua in (
        "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    ):
        check_mint(ua, FIREFOX)
        assert firefox_major(ua) == 152


def test_a_non_firefox_ua_has_no_major() -> None:
    assert firefox_major("curl/8.0") is None
