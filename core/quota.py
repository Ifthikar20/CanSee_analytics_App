"""
Per-user daily call-budget primitive backed by the Django cache.

Used by feature services (Perplexity search, Search Console, GA4) to
apply a flat daily cap on third-party API calls. Each service owns its
own ``namespace`` so budgets don't bleed across features.

Pattern:

    from core.quota import DailyQuota

    search_quota = DailyQuota(
        namespace="pplx_search",
        setting_name="PERPLEXITY_SEARCH_DAILY_LIMIT_PER_USER",
        default_limit=200,
    )

    if not search_quota.consume(user_id):
        return REFUSED
    ... make API call ...

    search_quota.remaining(user_id)   # how many calls left today
    search_quota.limit(user_id)        # what the cap is

The counter key is ``<namespace>:quota:<user_id|'anon'>:<YYYYMMDD>``
in the project cache (Redis in prod, locmem in tests). TTL is 26h so
the bucket survives the UTC-midnight rollover without leaving stale
data around for a full day.
"""
from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

# 26h gives us slack across UTC midnight + any clock skew on the cache
# servers. The key itself encodes the UTC date so old buckets are
# never read by a later day's request.
_TTL_SECONDS = 60 * 60 * 26


def _today_str() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


class DailyQuota:
    __slots__ = ("namespace", "setting_name", "default_limit")

    def __init__(self, *, namespace: str, setting_name: str, default_limit: int):
        self.namespace = namespace
        self.setting_name = setting_name
        self.default_limit = default_limit

    def _key(self, user_id) -> str:
        return f"{self.namespace}:quota:{user_id or 'anon'}:{_today_str()}"

    def limit(self, user_id=None) -> int:
        """Daily call allowance for ``user_id`` — same flat number for all."""
        return int(getattr(settings, self.setting_name, self.default_limit))

    def remaining(self, user_id=None) -> int:
        used = int(cache.get(self._key(user_id)) or 0)
        return max(0, self.limit(user_id) - used)

    def consume(self, user_id=None, n: int = 1) -> bool:
        """
        Reserve ``n`` calls. Returns False (and does NOT increment) when
        the call would exceed the daily cap.
        """
        key = self._key(user_id)
        used = int(cache.get(key) or 0)
        if used + n > self.limit(user_id):
            return False
        cache.set(key, used + n, timeout=_TTL_SECONDS)
        return True
