"""Согласованность отпечатка: одно семейство, пинится только мажор."""

from __future__ import annotations

import pytest

from mktlink.egress.fingerprint import (
    FIREFOX,
    PROFILES,
    FingerprintDrift,
    FingerprintProfile,
    check_mint,
    firefox_major,
    replay_headers,
)

FF_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"


def test_camoufox_is_firefox_so_impersonate_must_be_firefox() -> None:
    assert FIREFOX.minter == "camoufox"
    assert FIREFOX.impersonate.startswith("firefox")
    assert FIREFOX.sends_sec_ch_ua is False, "Firefox их не шлёт — иначе это тэлл"


def test_all_three_marketplaces_share_one_family() -> None:
    assert set(PROFILES) == {"ozon", "wb", "ym"}
    assert len({p.impersonate for p in PROFILES.values()}) == 1


def test_a_profile_whose_halves_disagree_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        FingerprintProfile("bad", "camoufox", "firefox133", 128, "ru", False)
    with pytest.raises(ValueError):
        FingerprintProfile("chrome-ua-on-firefox", "camoufox", "firefox133", 133, "ru", True)


def test_mint_check_binds_the_browser_build_to_the_profile() -> None:
    """Не тавтология: сверяется живой мажор сборки с числом в impersonate."""
    check_mint(FF_UA, FIREFOX)
    with pytest.raises(FingerprintDrift) as ei:
        check_mint(FF_UA.replace("133.0", "140.0").replace("rv:133", "rv:140"), FIREFOX)
    assert "bump" in str(ei.value), "сообщение должно говорить, что делать"


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


def test_only_the_major_is_pinned_so_rotation_does_not_break_minting() -> None:
    for ua in (
        "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    ):
        check_mint(ua, FIREFOX)
        assert firefox_major(ua) == 133


def test_a_non_firefox_ua_has_no_major() -> None:
    assert firefox_major("curl/8.0") is None
