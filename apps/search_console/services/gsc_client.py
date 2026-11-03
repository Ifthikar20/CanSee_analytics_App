"""
Google Search Console (Search Analytics) API client.

Thin OAuth-bearer client over the Webmasters v3 API: module-level
functions, quota guard, narrow exception handling, stats accounting.

Cost/failure guards:
  - Per-user daily quota (GSC_DAILY_API_LIMIT_PER_USER) on Search
    Analytics calls, so one tenant cannot drain the API budget.
  - Redis-backed circuit breaker shared across workers; a Google
    outage stops the nightly fan-out instead of hammering it.
  - query_all_rows bounds pagination by GSC_DAILY_ROW_LIMIT.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import requests
from django.conf import settings

from core.quota import DailyQuota
from core.resilience.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

logger = logging.getLogger("apps")

SITES_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"
QUERY_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"

# Google caps rowLimit at 25000 per request.
MAX_ROW_LIMIT = 25000
DEFAULT_DAILY_LIMIT_PER_USER = 2000

_breaker = CircuitBreaker(name="gsc", failure_threshold=5, recovery_timeout=120)

_quota = DailyQuota(
    namespace="gsc",
    setting_name="GSC_DAILY_API_LIMIT_PER_USER",
    default_limit=DEFAULT_DAILY_LIMIT_PER_USER,
)


def is_configured() -> bool:
    """True when the registry has Google OAuth credentials for gsc."""
    from core.integrations import get_registry

    cfg = get_registry().get("gsc")
    return bool(cfg and cfg.is_configured())


def _bump(stats: dict | None, key: str) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def list_sites(access_token: str, *, timeout: float = 10.0, stats: dict | None = None) -> list[dict]:
    """
    List Search Console properties the token can read.

    Returns [{"site_url": str, "permission_level": str}, ...]; [] on
    any failure (logged) or when the breaker is open.
    """
    if not access_token:
        return []
    if not _breaker.allow():
        _bump(stats, "breaker_blocks")
        return []
    try:
        resp = requests.get(
            SITES_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        _breaker.record_success()
        _bump(stats, "api_calls")
    except requests.RequestException as exc:
        _breaker.record_failure()
        _bump(stats, "errors")
        logger.warning("GSC list_sites failed: %s", exc)
        return []
    except ValueError as exc:
        _bump(stats, "errors")
        logger.warning("GSC list_sites returned non-JSON: %s", exc)
        return []

    return [
        {
            "site_url": entry.get("siteUrl", ""),
            "permission_level": entry.get("permissionLevel", ""),
        }
        for entry in data.get("siteEntry") or []
        if entry.get("siteUrl")
    ]


def query_search_analytics(
    access_token: str,
    site_url: str,
    *,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    row_limit: int = MAX_ROW_LIMIT,
    start_row: int = 0,
    user_id=None,
    stats: dict | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    """
    Run one Search Analytics query. Dates are YYYY-MM-DD strings.

    Returns Google's raw rows: [{"keys": [...], "clicks": ..,
    "impressions": .., "ctr": .., "position": ..}, ...]. Returns [] on
    quota exhaustion, open breaker, or any request failure (logged).
    """
    if not access_token or not site_url:
        return []
    if not _breaker.allow():
        _bump(stats, "breaker_blocks")
        return []
    if not _quota.consume(user_id, 1):
        _bump(stats, "quota_blocks")
        logger.warning("GSC daily quota exhausted for user %s", user_id)
        return []

    url = QUERY_ENDPOINT.format(site=quote(site_url, safe=""))
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": max(1, min(int(row_limit), MAX_ROW_LIMIT)),
        "startRow": max(0, int(start_row)),
    }
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        _breaker.record_success()
        _bump(stats, "api_calls")
    except requests.RequestException as exc:
        _breaker.record_failure()
        _bump(stats, "errors")
        logger.warning("GSC searchAnalytics query failed for %s: %s", site_url, exc)
        return []
    except ValueError as exc:
        _bump(stats, "errors")
        logger.warning("GSC searchAnalytics returned non-JSON for %s: %s", site_url, exc)
        return []

    return data.get("rows") or []


def query_all_rows(
    access_token: str,
    site_url: str,
    *,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    max_rows: int | None = None,
    user_id=None,
    stats: dict | None = None,
) -> list[dict]:
    """
    Paginate a Search Analytics query until a short page or max_rows.

    max_rows defaults to settings.GSC_DAILY_ROW_LIMIT.
    """
    limit = max_rows if max_rows is not None else getattr(settings, "GSC_DAILY_ROW_LIMIT", 5000)
    rows: list[dict] = []
    start_row = 0
    page_size = min(MAX_ROW_LIMIT, limit)

    while len(rows) < limit:
        want = min(page_size, limit - len(rows))
        page = query_search_analytics(
            access_token,
            site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            row_limit=want,
            start_row=start_row,
            user_id=user_id,
            stats=stats,
        )
        rows.extend(page)
        if len(page) < want:
            break
        start_row += len(page)

    return rows[:limit]


__all__ = [
    "CircuitBreakerOpen",
    "is_configured",
    "list_sites",
    "query_all_rows",
    "query_search_analytics",
]
