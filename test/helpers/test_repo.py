# Copyright 2025 Rudraksha Gupta
# SPDX-License-Identifier: GPL-3.0-or-later

from pmb.helpers.repo import get_repos_from_config


def test_get_repos_from_config_alpine_only(pmaports: None) -> None:
    """Devices with deviceinfo_alpine_only must not get a postmarketOS mirror."""
    get_repos_from_config.cache_clear()

    default = get_repos_from_config()
    alpine_only = get_repos_from_config(alpine_only=True)

    assert alpine_only, "expected at least Alpine's main/community"
    assert any("postmarketos" in url for url in default), (
        f"the default repo list should contain postmarketOS mirrors: {default}"
    )
    assert not any("postmarketos" in url for url in alpine_only), (
        f"alpine_only leaked a postmarketOS mirror: {alpine_only}"
    )
    assert all("alpinelinux.org" in url for url in alpine_only), (
        f"alpine_only returned a non-Alpine mirror: {alpine_only}"
    )

    # Alpine's edge has three repositories
    assert [url.rsplit("/", 1)[1] for url in alpine_only] == ["main", "community", "testing"]


def test_get_repos_from_config_caching(pmaports: None) -> None:
    """get_repos_from_config() used to mutate its own mutable default argument
    (mirrors_exclude.append("systemd")), which changed the @Cache key on every
    call and therefore never hit the cache.
    """
    get_repos_from_config.cache_clear()

    first = get_repos_from_config()
    second = get_repos_from_config()
    third = get_repos_from_config()

    assert first == second == third
    assert get_repos_from_config.hits == 2, (
        f"expected 2 cache hits, got {get_repos_from_config.hits}"
        f" (misses: {get_repos_from_config.misses})"
    )

    # ...and alpine_only must be part of the cache key, not silently ignored
    assert get_repos_from_config(alpine_only=True) != first
