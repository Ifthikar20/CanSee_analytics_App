"""Cloudflare rollup report for the traffic pages (pages contract 4c, 4d).

One GraphQL document over httpRequests1dGroups (daily rollups, about a
year on free zones, not sampled) answers every Cloudflare number the
traffic page shows besides the sampled breakdowns: requests and page
views per day, the window's totals with every country, and the previous
window's totals for the change. The dashboard bundle cannot stand in:
it reports unique IPs as visitors and folds requests into page views.

``clientCountryName`` values are ISO-2 codes; XX (unknown) and T1 (Tor)
count as unknown. Cached ten minutes per website and zone.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.web_analytics.services import cloudflare_client, cloudflare_dashboard, snapshots

REPORT_TTL_SECONDS = 600
UNKNOWN_COUNTRIES = frozenset({"XX", "T1"})

REPORT_QUERY = """
query ($zone: String!, $since: Date!, $until: Date!, $prevSince: Date!, $prevUntil: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    days: httpRequests1dGroups(limit: 400, filter: {date_geq: $since, date_leq: $until}) {
      dimensions { date }
      sum { requests pageViews }
    }
    totals: httpRequests1dGroups(limit: 1, filter: {date_geq: $since, date_leq: $until}) {
      sum { requests pageViews countryMap { clientCountryName requests } }
    }
    previous: httpRequests1dGroups(limit: 1, filter: {date_geq: $prevSince, date_leq: $prevUntil}) {
      sum { requests pageViews }
    }
  } }
}
"""


def window(period: str):
    """(since, until, previous since, previous until) dates, the window
    cloudflare_dashboard uses for daily periods."""
    days = cloudflare_dashboard.DAILY_PERIODS[period]
    today = timezone.now().date()
    since = today - timedelta(days=days - 1)
    return since, today, since - timedelta(days=days), since - timedelta(days=1)


def _sums(groups) -> dict:
    total = ((groups or [None])[0] or {}).get("sum") or {}
    return {
        "requests": int(total.get("requests") or 0),
        "pageviews": int(total.get("pageViews") or 0),
    }


def _fetch(api_token: str, zone_id: str, period: str) -> dict | None:
    since, until, prev_since, prev_until = window(period)
    data = cloudflare_client._post_graphql(api_token, REPORT_QUERY, {
        "zone": zone_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "prevSince": prev_since.isoformat(),
        "prevUntil": prev_until.isoformat(),
    })
    zones = ((data or {}).get("viewer") or {}).get("zones") or []
    if not zones:
        return None
    zone = zones[0]

    by_day = {}
    for group in zone.get("days") or []:
        day = (group.get("dimensions") or {}).get("date", "")
        if day:
            by_day[day] = _sums([group])
    labels = [(since + timedelta(days=i)).isoformat() for i in range((until - since).days + 1)]
    empty = {"requests": 0, "pageviews": 0}

    countries, unknown = [], 0
    totals_groups = zone.get("totals") or []
    country_map = ((totals_groups[0] if totals_groups else {}).get("sum") or {}).get("countryMap")
    for row in country_map or []:
        code = str(row.get("clientCountryName") or "").strip().upper()
        requests = int(row.get("requests") or 0)
        if len(code) == 2 and code.isalpha() and code not in UNKNOWN_COUNTRIES:
            countries.append([code, requests])
        else:
            unknown += requests

    return {
        "labels": labels,
        "requests": [by_day.get(day, empty)["requests"] for day in labels],
        "pageviews": [by_day.get(day, empty)["pageviews"] for day in labels],
        "totals": _sums(totals_groups),
        "previous": _sums(zone.get("previous")),
        "countries": countries,
        "unknown": unknown,
    }


def report(integration, website, period: str) -> dict | None:
    """{labels, requests, pageviews, totals, previous, countries: [[code,
    requests]], unknown, stale}, or None when Cloudflare failed with no
    saved copy."""
    metadata = integration.metadata or {}
    zone_id = metadata.get("zone_id")
    api_token = integration.access_token
    if not zone_id or not api_token:
        return None
    payload = snapshots.get_or_fetch(
        f"cftr:{period}:{zone_id}", website.id,
        lambda: _fetch(api_token, zone_id, period), ttl=REPORT_TTL_SECONDS,
    )
    if not isinstance(payload, dict) or payload.get("pending"):
        return None
    return payload


def countries(integration, website, period: str) -> dict | None:
    """{"countries": [[code, requests]], "unknown": requests, "stale"}: the
    country part of the same report (one upstream call serves both)."""
    found = report(integration, website, period)
    if found is None:
        return None
    return {"countries": found["countries"], "unknown": found["unknown"],
            "stale": found.get("stale", False)}
