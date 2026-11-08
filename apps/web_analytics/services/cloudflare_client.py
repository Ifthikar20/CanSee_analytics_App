"""
Cloudflare zone analytics client (GraphQL + two REST helpers).

Tenants supply their own API token (Zone -> Analytics -> Read); it is
stored encrypted on Integration(type="cloudflare") and never echoed
back through the API. The site must be orange-cloud proxied for
httpRequestsAdaptiveGroups to contain anything.

Honesty notes baked into the payload: edge data lags 1-5 minutes, and
the adaptive dataset is SAMPLED — counts are estimated by multiplying
by the average sampleInterval, so everything is labeled approximate in
the UI. Ranges stay within 24h to fit free-plan retention.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from core.resilience.circuit_breaker import CircuitBreaker

logger = logging.getLogger("apps")

GRAPHQL_ENDPOINT = "https://api.cloudflare.com/client/v4/graphql"
VERIFY_ENDPOINT = "https://api.cloudflare.com/client/v4/user/tokens/verify"
ZONES_ENDPOINT = "https://api.cloudflare.com/client/v4/zones"

WINDOW_MINUTES = 30
MAX_ZONE_PAGES = 4  # 50 zones/page; nobody connecting here has 200+ zones

_breaker = CircuitBreaker(name="cloudflare", failure_threshold=5, recovery_timeout=120)

_SNAPSHOT_QUERY = """
query ($zone: String!, $since30: Time!, $since24: Time!, $now: Time!) {
  viewer {
    zones(filter: {zoneTag: $zone}) {
      perMinute: httpRequestsAdaptiveGroups(
        limit: 40,
        filter: {datetime_geq: $since30, datetime_leq: $now}
      ) {
        count
        avg { sampleInterval }
        sum { visits, edgeResponseBytes }
        dimensions { datetimeMinute }
      }
      totals: httpRequestsAdaptiveGroups(
        limit: 1,
        filter: {datetime_geq: $since24, datetime_leq: $now}
      ) {
        count
        avg { sampleInterval }
        sum { visits, edgeResponseBytes }
      }
      countries: httpRequestsAdaptiveGroups(
        limit: 10,
        filter: {datetime_geq: $since24, datetime_leq: $now},
        orderBy: [sum_visits_DESC]
      ) {
        count
        avg { sampleInterval }
        sum { visits }
        dimensions { clientCountryName }
      }
    }
  }
}
"""


def _bump(stats: dict | None, key: str) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def _timeout() -> float:
    return float(getattr(settings, "CLOUDFLARE_FETCH_TIMEOUT_SECONDS", 10.0))


def verify_token(api_token: str) -> bool:
    """True when Cloudflare reports the token valid and active."""
    if not api_token:
        return False
    try:
        resp = requests.get(
            VERIFY_ENDPOINT,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=_timeout(),
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Cloudflare token verify failed: %s", exc)
        return False
    return bool(data.get("success")) and (data.get("result") or {}).get("status") == "active"


def list_zones(api_token: str, *, stats: dict | None = None) -> list[dict]:
    """Zones the token can see: [{"id", "name", "status", "paused"}].

    [] on failure (logged) — including tokens scoped to analytics only
    without Zone->Zone->Read; the connect flow treats an empty list as
    "paste your Zone ID manually is not supported, re-scope the token".
    """
    if not api_token:
        return []
    zones: list[dict] = []
    for page in range(1, MAX_ZONE_PAGES + 1):
        if not _breaker.allow():
            _bump(stats, "breaker_blocks")
            return zones
        try:
            resp = requests.get(
                ZONES_ENDPOINT,
                params={"page": page, "per_page": 50},
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=_timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
            _breaker.record_success()
            _bump(stats, "api_calls")
        except requests.RequestException as exc:
            _breaker.record_failure()
            _bump(stats, "errors")
            logger.warning("Cloudflare list_zones failed: %s", exc)
            return zones
        except ValueError as exc:
            _bump(stats, "errors")
            logger.warning("Cloudflare list_zones returned non-JSON: %s", exc)
            return zones

        for zone in data.get("result") or []:
            if zone.get("id") and zone.get("name"):
                zones.append({
                    "id": zone["id"],
                    "name": zone["name"],
                    "status": zone.get("status", ""),
                    "paused": bool(zone.get("paused")),
                })

        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break

    return zones


def _post_graphql(
    api_token: str,
    query: str,
    variables: dict,
    *,
    stats: dict | None = None,
    errors: list | None = None,
) -> dict | None:
    """The one GraphQL seam. Returns the "data" dict or None (logged).

    ``errors``, when given, receives the GraphQL error messages of a
    response that carried any (a failed request adds nothing), so a caller
    can tell "the plan refused this query" from "Cloudflare was down".
    """
    if not _breaker.allow():
        _bump(stats, "breaker_blocks")
        return None
    try:
        resp = requests.post(
            GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        _breaker.record_success()
        _bump(stats, "api_calls")
    except requests.RequestException as exc:
        _breaker.record_failure()
        _bump(stats, "errors")
        logger.warning("Cloudflare GraphQL request failed: %s", exc)
        return None
    except ValueError as exc:
        _bump(stats, "errors")
        logger.warning("Cloudflare GraphQL returned non-JSON: %s", exc)
        return None

    if data.get("errors"):
        _bump(stats, "errors")
        logger.warning("Cloudflare GraphQL errors: %s", str(data["errors"])[:500])
        if errors is not None:
            for error in data["errors"]:
                message = error.get("message") if isinstance(error, dict) else error
                errors.append(str(message or ""))
        return None
    return data.get("data")


def _estimate(group: dict) -> dict:
    """Un-sample one adaptive group: multiply by avg sampleInterval."""
    interval = float((group.get("avg") or {}).get("sampleInterval") or 1) or 1.0
    total = group.get("sum") or {}
    return {
        "requests": int(round(float(group.get("count") or 0) * interval)),
        "visits": int(round(float(total.get("visits") or 0) * interval)),
        "bytes": int(round(float(total.get("edgeResponseBytes") or 0) * interval)),
    }


def build_zone_snapshot(api_token: str, zone_id: str, *, stats: dict | None = None) -> dict | None:
    """Compose the zone snapshot (one GraphQL document, three groupings)."""
    if not api_token or not zone_id:
        return None

    now = timezone.now()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    data = _post_graphql(
        api_token,
        _SNAPSHOT_QUERY,
        {
            "zone": zone_id,
            "since30": (now - timedelta(minutes=WINDOW_MINUTES)).strftime(fmt),
            "since24": (now - timedelta(hours=24)).strftime(fmt),
            "now": now.strftime(fmt),
        },
        stats=stats,
    )
    if data is None:
        return None

    zones = (data.get("viewer") or {}).get("zones") or []
    if not zones:
        logger.warning("Cloudflare GraphQL returned no zone for %s", zone_id)
        return None
    zone = zones[0]

    per_minute = []
    for group in zone.get("perMinute") or []:
        minute = (group.get("dimensions") or {}).get("datetimeMinute", "")
        if not minute:
            continue
        per_minute.append({"minute": minute, **_estimate(group)})
    per_minute.sort(key=lambda row: row["minute"])

    totals_groups = zone.get("totals") or []
    totals = _estimate(totals_groups[0]) if totals_groups else {"requests": 0, "visits": 0, "bytes": 0}

    countries = []
    for group in zone.get("countries") or []:
        name = (group.get("dimensions") or {}).get("clientCountryName", "")
        if not name:
            continue
        countries.append({"country": name, **_estimate(group)})

    return {
        "source": "cloudflare",
        "zone_id": zone_id,
        "per_minute": per_minute,
        "totals_24h": totals,
        "countries": countries,
        "window_minutes": WINDOW_MINUTES,
        "sampled": True,
        "fetched_at": now.isoformat(),
    }


__all__ = [
    "build_zone_snapshot",
    "list_zones",
    "verify_token",
]
