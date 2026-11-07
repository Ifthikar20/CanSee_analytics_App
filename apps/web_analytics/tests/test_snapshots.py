"""Unit tests for the read-through snapshot cache."""

from unittest.mock import MagicMock

import pytest
from django.core.cache import cache

from apps.web_analytics.services import snapshots


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_fetches_and_caches_on_miss():
    fetch = MagicMock(return_value={"active_users": 5})
    out = snapshots.get_or_fetch("ga4", "w1", fetch, ttl=25)

    assert out == {"active_users": 5, "stale": False}
    fetch.assert_called_once()

    # Second call is served from cache without touching upstream.
    out2 = snapshots.get_or_fetch("ga4", "w1", fetch, ttl=25)
    assert out2 == out
    fetch.assert_called_once()


def test_serves_last_snapshot_when_fetch_fails():
    snapshots.get_or_fetch("ga4", "w1", lambda: {"active_users": 5}, ttl=25)
    cache.delete("wa:ga4:w1")  # simulate TTL expiry, keep the :last copy

    out = snapshots.get_or_fetch("ga4", "w1", lambda: None, ttl=25)
    assert out["active_users"] == 5
    assert out["stale"] is True


def test_serves_last_snapshot_when_fetch_raises():
    snapshots.get_or_fetch("ga4", "w1", lambda: {"active_users": 5}, ttl=25)
    cache.delete("wa:ga4:w1")

    def boom():
        raise RuntimeError("upstream exploded")

    out = snapshots.get_or_fetch("ga4", "w1", boom, ttl=25)
    assert out["stale"] is True
    assert out["active_users"] == 5


def test_pending_when_nothing_cached_and_fetch_fails():
    out = snapshots.get_or_fetch("ga4", "w1", lambda: None, ttl=25)
    assert out == {"pending": True, "stale": True}


def test_lock_holder_elsewhere_serves_stale_without_fetching():
    snapshots.get_or_fetch("ga4", "w1", lambda: {"active_users": 5}, ttl=25)
    cache.delete("wa:ga4:w1")
    cache.set("wa:ga4:w1:lock", 1, 10)  # someone else is fetching

    fetch = MagicMock(return_value={"active_users": 99})
    out = snapshots.get_or_fetch("ga4", "w1", fetch, ttl=25)

    fetch.assert_not_called()
    assert out["active_users"] == 5
    assert out["stale"] is True


def test_lock_released_after_fetch():
    snapshots.get_or_fetch("ga4", "w1", lambda: {"n": 1}, ttl=25)
    assert cache.get("wa:ga4:w1:lock") is None


def test_invalidate_drops_both_copies():
    snapshots.get_or_fetch("ga4", "w1", lambda: {"n": 1}, ttl=25)
    snapshots.invalidate("ga4", "w1")
    assert cache.get("wa:ga4:w1") is None
    assert cache.get("wa:ga4:w1:last") is None
