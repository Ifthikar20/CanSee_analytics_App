"""
Cloudflare-backed dashboard bundle.

Populates every Overview card the zone's plan can actually serve, and
leaves genuinely-unavailable slices empty so the frontend hides those
card types. Field availability was probed live against a free-plan zone
(settings.<dataset>.availableFields is the authz truth — schema
introspection alone lists fields the plan cannot query):

  - httpRequests1hGroups / 1dGroups (free): real `uniq.uniques`,
    `sum.pageViews/requests`, `countryMap`, `browserMap
    {uaBrowserFamily, pageViews}`. 1h retention ~3 days, 1d ~1 year.
  - httpRequests1mGroups: DISABLED on free zones — minute-class periods
    fall back to adaptive-group estimates (visits/requests, no uniques).
  - httpRequestsAdaptiveGroups (free): clientRequestPath,
    clientDeviceType, userAgentOS, userAgentBrowser, clientCountryName,
    sum.visits, count. Range capped at settings.maxDuration (1 day
    free). NOT available on free: clientRefererHost (so Top Sources
    stays empty for Cloudflare) and edgeResponseContentTypeName (so Top
    Pages relies on the asset-extension post-filter).

Honesty rules: pageViews is Cloudflare's HTML-page-load metric — for
SPAs and bot-only zones it is legitimately 0, in which case the request
count stands in (the numbers are labeled sampled estimates in the UI).
Trends always compare the same metric across both windows.

Per bundle build: 3-4 GraphQL calls with isolated failure semantics
(+1 settings call cached an hour per zone). Main-call failure -> bundle
None (stale/pixel fallback); any other call failing empties only its
own slices.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.web_analytics.services.cloudflare_client import _estimate, _post_graphql
from apps.web_analytics.services.ga4_dashboard import DEVICE_COLORS

logger = logging.getLogger("apps")

SOURCE_NAME = "cloudflare"

MINUTE_PERIODS = {"5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
HOURLY_PERIODS = {"6h": 6, "24h": 24}
DAILY_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "6mo": 182}

# Conservative free-plan defaults, used when the settings query fails.
DEFAULT_DATASET_SETTINGS = {
    "adaptive": {"enabled": True, "maxDuration": 86400, "notOlderThan": 691200},
    "m1": {"enabled": False, "maxDuration": 0, "notOlderThan": 0},
    "h1": {"enabled": True, "maxDuration": 259200, "notOlderThan": 262800},
    "d1": {"enabled": True, "maxDuration": 31539600, "notOlderThan": 31539600},
}

_SETTINGS_TTL_SECONDS = 3600
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

_ASSET_RE = re.compile(
    r"\.(js|mjs|css|map|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|otf|eot|json|xml|txt|pdf|mp4|webm)$"
    r"|^/cdn-cgi/",
    re.IGNORECASE,
)

# Attack-probe classifier for the Flagged Paths card. These are paths a
# legitimate visitor of a marketing/app site never requests; seeing them
# in edge traffic means scanners are probing the site. Ordered — first
# match wins.
_SUSPICIOUS_PATTERNS = (
    (re.compile(r"/(wp-admin|wp-login|wp-content|wp-includes|xmlrpc\.php)", re.I), "WordPress probe"),
    (re.compile(r"/(\.env|\.git|\.aws|\.ssh|\.docker|config\.(php|json|ya?ml)|credentials|secrets?)($|[/?.])|\.(sql|bak|backup|old)$", re.I), "Secrets/config probe"),
    (re.compile(r"/(phpmyadmin|adminer|administrator|admin[0-9]*|manager|console|actuator|_profiler|phpinfo|cpanel|webadmin|dbadmin)($|[/?.])", re.I), "Admin scan"),
    (re.compile(r"/(shell|cmd|eval|cgi-bin|vendor/phpunit|boaform|HNAP1|solr|jenkins|struts|weblogic|fckeditor|filemanager)($|[/?._-])", re.I), "Shell/exploit probe"),
)


def _classify_suspicious(path: str) -> str | None:
    for pattern, category in _SUSPICIOUS_PATTERNS:
        if pattern.search(path or ""):
            return category
    return None

_SETTINGS_QUERY = """
query ($zone: String!) {
  viewer { zones(filter: {zoneTag: $zone}) { settings {
    adaptive: httpRequestsAdaptiveGroups { enabled, maxDuration, notOlderThan }
    m1: httpRequests1mGroups { enabled, maxDuration, notOlderThan }
    h1: httpRequests1hGroups { enabled, maxDuration, notOlderThan }
    d1: httpRequests1dGroups { enabled, maxDuration, notOlderThan }
  } } }
}
"""

# Rollup main/prev/maps documents, %-parametrized by dataset + time
# dimension. Datetime-filtered datasets (1m/1h) use Time! variables;
# the 1d rollup uses Date! variables.
_ROLLUP_MAIN_TIME = """
query ($zone: String!, $since: Time!, $now: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    series: %(dataset)s(limit: 400, filter: {datetime_geq: $since, datetime_lt: $now}) {
      dimensions { %(dim)s }
      sum { pageViews, requests }
      uniq { uniques }
    }
    totals: %(dataset)s(limit: 1, filter: {datetime_geq: $since, datetime_lt: $now}) {
      sum { pageViews, requests }
      uniq { uniques }
    }
  } }
}
"""

_ROLLUP_MAIN_DATE = """
query ($zone: String!, $since: Date!, $until: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    series: httpRequests1dGroups(limit: 400, filter: {date_geq: $since, date_leq: $until}) {
      dimensions { date }
      sum { pageViews, requests }
      uniq { uniques }
    }
    totals: httpRequests1dGroups(limit: 1, filter: {date_geq: $since, date_leq: $until}) {
      sum { pageViews, requests }
      uniq { uniques }
    }
  } }
}
"""

_ROLLUP_PREV_TIME = """
query ($zone: String!, $since: Time!, $until: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    previous: %(dataset)s(limit: 1, filter: {datetime_geq: $since, datetime_lt: $until}) {
      sum { pageViews, requests }
      uniq { uniques }
    }
  } }
}
"""

_ROLLUP_PREV_DATE = """
query ($zone: String!, $since: Date!, $until: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    previous: httpRequests1dGroups(limit: 1, filter: {date_geq: $since, date_leq: $until}) {
      sum { pageViews, requests }
      uniq { uniques }
    }
  } }
}
"""

_ROLLUP_MAPS_TIME = """
query ($zone: String!, $since: Time!, $now: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    maps: %(dataset)s(limit: 1, filter: {datetime_geq: $since, datetime_lt: $now}) {
      sum {
        threats
        countryMap { clientCountryName, requests }
        browserMap { uaBrowserFamily, pageViews }
        threatPathingMap { threatPathingName, requests }
      }
    }
  } }
}
"""

_ROLLUP_MAPS_DATE = """
query ($zone: String!, $since: Date!, $until: Date!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    maps: httpRequests1dGroups(limit: 1, filter: {date_geq: $since, date_leq: $until}) {
      sum {
        threats
        countryMap { clientCountryName, requests }
        browserMap { uaBrowserFamily, pageViews }
        threatPathingMap { threatPathingName, requests }
      }
    }
  } }
}
"""

# Adaptive documents. Minute-class KPIs/chart (1mGroups is disabled on
# free zones) and the breakdown groupings for every class.
_ADAPTIVE_MAIN = """
query ($zone: String!, $since: Time!, $now: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    series: httpRequestsAdaptiveGroups(limit: 400, filter: {datetime_geq: $since, datetime_leq: $now}) {
      count
      avg { sampleInterval }
      sum { visits }
      dimensions { datetimeMinute }
    }
    totals: httpRequestsAdaptiveGroups(limit: 1, filter: {datetime_geq: $since, datetime_leq: $now}) {
      count
      avg { sampleInterval }
      sum { visits }
    }
  } }
}
"""

_ADAPTIVE_PREV = """
query ($zone: String!, $since: Time!, $until: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    previous: httpRequestsAdaptiveGroups(limit: 1, filter: {datetime_geq: $since, datetime_leq: $until}) {
      count
      avg { sampleInterval }
      sum { visits }
    }
  } }
}
"""

_ADAPTIVE_BREAKDOWNS = """
query ($zone: String!, $since: Time!, $now: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    pages: httpRequestsAdaptiveGroups(limit: 25, filter: {datetime_geq: $since, datetime_leq: $now}, orderBy: [count_DESC]) {
      count
      avg { sampleInterval }
      dimensions { clientRequestPath }
    }
    deviceTypes: httpRequestsAdaptiveGroups(limit: 5, filter: {datetime_geq: $since, datetime_leq: $now}, orderBy: [count_DESC]) {
      count
      avg { sampleInterval }
      dimensions { clientDeviceType }
    }
    oses: httpRequestsAdaptiveGroups(limit: 8, filter: {datetime_geq: $since, datetime_leq: $now}, orderBy: [count_DESC]) {
      count
      avg { sampleInterval }
      dimensions { userAgentOS }
    }
    browsers: httpRequestsAdaptiveGroups(limit: 8, filter: {datetime_geq: $since, datetime_leq: $now}, orderBy: [count_DESC]) {
      count
      avg { sampleInterval }
      dimensions { userAgentBrowser }
    }
    countries: httpRequestsAdaptiveGroups(limit: 10, filter: {datetime_geq: $since, datetime_leq: $now}, orderBy: [count_DESC]) {
      count
      avg { sampleInterval }
      dimensions { clientCountryName }
    }
  } }
}
"""


def _trend(current: float, previous: float) -> float | None:
    # Nothing in the previous window means the change is unknown, not 0%.
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _pct(part: float, total: float) -> float:
    if not total:
        return 0.0
    return round(part / total * 100, 1)


def _zone(data) -> dict | None:
    zones = ((data or {}).get("viewer") or {}).get("zones") or []
    return zones[0] if zones else None


def _overview(period: str, **values) -> dict:
    base = {
        "period": period,
        "total_visitors": 0,
        "total_pageviews": 0,
        "visitor_growth_pct": None,
        "pageviews_trend": None,
        # No session model at the edge, so these stay None.
        "avg_session": None,
        "session_trend": None,
        "bounce_rate": None,
        "bounce_trend": None,
        "realtime": 0,
        "data_source": SOURCE_NAME,
    }
    base.update(values)
    return base


def _zone_settings(api_token: str, zone_id: str, stats=None) -> dict:
    """Per-dataset {enabled, maxDuration, notOlderThan}, cached an hour."""
    key = f"wa:cfsettings:{zone_id}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    result = {k: dict(v) for k, v in DEFAULT_DATASET_SETTINGS.items()}
    zone = _zone(_post_graphql(api_token, _SETTINGS_QUERY, {"zone": zone_id}, stats=stats))
    if zone and zone.get("settings"):
        for name, values in zone["settings"].items():
            if isinstance(values, dict) and name in result:
                result[name].update({k: v for k, v in values.items() if v is not None})
    try:
        cache.set(key, result, _SETTINGS_TTL_SECONDS)
    except Exception:
        pass
    return result


def _looks_like_asset(path: str) -> bool:
    return bool(_ASSET_RE.search(path or ""))


def _named_estimates(groups, dim: str, *, limit: int, skip=lambda name: False) -> list[dict]:
    """Adaptive groups -> [{name, count, pct}] by estimated requests."""
    rows = []
    for group in groups or []:
        name = (group.get("dimensions") or {}).get(dim, "")
        if not name or skip(name):
            continue
        rows.append((name, _estimate(group)["requests"]))
    rows = rows[:limit]
    total = sum(count for _n, count in rows) or 1
    return [{"name": name, "count": count, "pct": _pct(count, total)} for name, count in rows]


def _rollup_totals(group) -> dict:
    total = (group or {}).get("sum") or {}
    return {
        "pageviews": int(total.get("pageViews") or 0),
        "requests": int(total.get("requests") or 0),
        "uniques": int(((group or {}).get("uniq") or {}).get("uniques") or 0),
    }


def _skeleton(bucket_kind: str, now, count: int) -> list[dict]:
    """Zero-filled chart buckets keyed by the dataset's dimension value."""
    buckets = []
    if bucket_kind == "minute":
        base = now.replace(second=0, microsecond=0)
        for i in range(count - 1, -1, -1):
            slot = base - timedelta(minutes=i)
            buckets.append({
                "key": slot.strftime("%Y-%m-%dT%H:%M:00Z"),
                "label": slot.strftime("%H:%M"),
                "visitors": 0, "pageviews": 0, "sessions": 0,
            })
    elif bucket_kind == "hour":
        base = now.replace(minute=0, second=0, microsecond=0)
        for i in range(count - 1, -1, -1):
            slot = base - timedelta(hours=i)
            buckets.append({
                "key": slot.strftime("%Y-%m-%dT%H:00:00Z"),
                "label": f"{slot.hour:02d}:00",
                "visitors": 0, "pageviews": 0, "sessions": 0,
            })
    else:
        today = now.date()
        for i in range(count - 1, -1, -1):
            day = today - timedelta(days=i)
            buckets.append({
                "key": day.isoformat(),
                "date": day.isoformat(),
                "label": f"{day:%b} {day.day}",
                "visitors": 0, "pageviews": 0, "sessions": 0,
            })
    return buckets


def _breakdown_slices(api_token, zone_id, since, now, stats) -> dict:
    """Adaptive breakdowns: pages, devices, OS, browsers, countries.
    One doc; failure empties all five (KPIs untouched)."""
    empty = {
        "pages": [], "devices": [], "operating_systems": [],
        "browsers": [], "countries": [], "flagged": [],
    }
    zone = _zone(_post_graphql(
        api_token,
        _ADAPTIVE_BREAKDOWNS,
        {"zone": zone_id, "since": since.strftime(_TIME_FMT), "now": now.strftime(_TIME_FMT)},
        stats=stats,
    ))
    if zone is None:
        return empty

    # One path grouping feeds two cards: attack-probe paths go to the
    # Flagged Paths card, everything else (minus assets) to Top Pages.
    pages, flagged = [], []
    for group in zone.get("pages") or []:
        path = (group.get("dimensions") or {}).get("clientRequestPath", "")
        if not path:
            continue
        category = _classify_suspicious(path)
        if category:
            flagged.append({"url": path, "requests": _estimate(group)["requests"], "category": category})
            continue
        if _looks_like_asset(path):
            continue
        pages.append({"url": path, "views": _estimate(group)["requests"]})
    pages = pages[:10]
    flagged = flagged[:8]

    devices = _named_estimates(zone.get("deviceTypes"), "clientDeviceType", limit=5)
    for item in devices:
        raw = item["name"].lower()
        item["name"] = item["name"].capitalize()
        item["color"] = DEVICE_COLORS.get(raw, "var(--text-muted)")

    return {
        "pages": pages,
        "flagged": flagged,
        "devices": devices,
        "operating_systems": _named_estimates(zone.get("oses"), "userAgentOS", limit=8),
        "browsers": _named_estimates(zone.get("browsers"), "userAgentBrowser", limit=8),
        "countries": [
            {"name": row["name"], "pct": row["pct"], "visitors": row["count"]}
            for row in _named_estimates(zone.get("countries"), "clientCountryName", limit=10)
        ],
    }


def _map_slices(api_token, zone_id, query, variables, stats) -> dict | None:
    """Rollup countryMap/browserMap. None on failure (caller falls back)."""
    zone = _zone(_post_graphql(api_token, query, variables, stats=stats))
    if zone is None:
        return None
    groups = zone.get("maps") or []
    total = (groups[0].get("sum") or {}) if groups else {}

    country_rows = sorted(
        (
            (row.get("clientCountryName", ""), int(row.get("requests") or 0))
            for row in total.get("countryMap") or []
            if row.get("clientCountryName")
        ),
        key=lambda r: -r[1],
    )[:10]
    country_total = sum(v for _n, v in country_rows) or 1
    browser_rows = sorted(
        (
            (row.get("uaBrowserFamily", ""), int(row.get("pageViews") or 0))
            for row in total.get("browserMap") or []
            if row.get("uaBrowserFamily")
        ),
        key=lambda r: -r[1],
    )[:8]
    browser_total = sum(v for _n, v in browser_rows) or 1
    threat_rows = sorted(
        (
            (row.get("threatPathingName", ""), int(row.get("requests") or 0))
            for row in total.get("threatPathingMap") or []
            if row.get("threatPathingName")
        ),
        key=lambda r: -r[1],
    )[:6]

    return {
        "countries": [
            {"name": name, "pct": _pct(requests, country_total), "visitors": requests}
            for name, requests in country_rows
        ],
        "browsers": [
            {"name": name, "count": views, "pct": _pct(views, browser_total)}
            for name, views in browser_rows
        ],
        "threats": int(total.get("threats") or 0),
        "threat_categories": [
            {"name": name, "requests": requests} for name, requests in threat_rows
        ],
    }


def _build_from_rollups(api_token, zone_id, period, dataset_key, zone_settings, stats):
    """HOURLY (1h rollups) and DAILY (1d rollups) period classes."""
    now = timezone.now()

    if dataset_key == "d1":
        days = DAILY_PERIODS[period]
        today = now.date()
        since_d = today - timedelta(days=days - 1)
        main_vars = {"zone": zone_id, "since": since_d.isoformat(), "until": today.isoformat()}
        prev_vars = {
            "zone": zone_id,
            "since": (since_d - timedelta(days=days)).isoformat(),
            "until": (since_d - timedelta(days=1)).isoformat(),
        }
        main_q, prev_q, maps_q = _ROLLUP_MAIN_DATE, _ROLLUP_PREV_DATE, _ROLLUP_MAPS_DATE
        chart = _skeleton("day", now, days)
        dim = "date"
    else:
        hours = HOURLY_PERIODS[period]
        since_t = now - timedelta(hours=hours)
        main_vars = {"zone": zone_id, "since": since_t.strftime(_TIME_FMT), "now": now.strftime(_TIME_FMT)}
        prev_vars = {
            "zone": zone_id,
            "since": (since_t - timedelta(hours=hours)).strftime(_TIME_FMT),
            "until": since_t.strftime(_TIME_FMT),
        }
        sub = {"dataset": "httpRequests1hGroups", "dim": "datetime"}
        main_q, prev_q = _ROLLUP_MAIN_TIME % sub, _ROLLUP_PREV_TIME % sub
        maps_q = _ROLLUP_MAPS_TIME % sub
        chart = _skeleton("hour", now, hours)
        dim = "datetime"

    zone = _zone(_post_graphql(api_token, main_q, main_vars, stats=stats))
    if zone is None:
        return None

    totals_groups = zone.get("totals") or []
    current = _rollup_totals(totals_groups[0] if totals_groups else None)
    # Cloudflare pageViews counts HTML page loads only — legitimately 0
    # for SPAs and bot-only zones; requests stand in so the card is
    # never blank for a zone that clearly has traffic.
    use_requests = current["pageviews"] == 0 and current["requests"] > 0
    pageviews_of = (lambda t: t["requests"]) if use_requests else (lambda t: t["pageviews"])

    previous = {"pageviews": 0, "requests": 0, "uniques": 0}
    prev_zone = _zone(_post_graphql(api_token, prev_q, prev_vars, stats=stats))
    if prev_zone and prev_zone.get("previous"):
        previous = _rollup_totals(prev_zone["previous"][0])

    by_key = {b["key"]: b for b in chart}
    for group in zone.get("series") or []:
        bucket = by_key.get((group.get("dimensions") or {}).get(dim, ""))
        if bucket is None:
            continue
        totals = _rollup_totals(group)
        bucket["visitors"] = totals["uniques"]
        bucket["pageviews"] = pageviews_of(totals)
    for bucket in chart:
        bucket.pop("key", None)

    # Breakdown window: adaptive is plan-capped (1 day on free zones).
    adaptive = zone_settings["adaptive"]
    window_seconds = min(
        int(timedelta(days=DAILY_PERIODS[period]).total_seconds()) if dataset_key == "d1"
        else HOURLY_PERIODS.get(period, 24) * 3600,
        int(adaptive.get("maxDuration") or 86400),
    )
    breakdown_since = now - timedelta(seconds=window_seconds)
    slices = _breakdown_slices(api_token, zone_id, breakdown_since, now, stats)

    maps = _map_slices(api_token, zone_id, maps_q, main_vars, stats)
    security = {"flagged": slices["flagged"], "threats": 0, "threat_categories": []}
    if maps is not None:
        if maps["countries"]:
            slices["countries"] = maps["countries"]
        if maps["browsers"]:
            slices["browsers"] = maps["browsers"]
        security["threats"] = maps["threats"]
        security["threat_categories"] = maps["threat_categories"]

    return {
        "overview": _overview(
            period,
            total_visitors=current["uniques"],
            total_pageviews=pageviews_of(current),
            visitor_growth_pct=_trend(current["uniques"], previous["uniques"]),
            pageviews_trend=_trend(pageviews_of(current), pageviews_of(previous)),
            breakdown_window_hours=window_seconds // 3600,
            security=security,
        ),
        "chart": chart,
        "pages": slices["pages"],
        # clientRefererHost is not queryable on free zones: no referrer
        # data exists, so the frontend hides the source cards entirely.
        "sources": [],
        "devices": {
            "devices": slices["devices"],
            "browsers": slices["browsers"],
            "operating_systems": slices["operating_systems"],
        },
        "countries": slices["countries"],
    }


def _build_from_adaptive(api_token, zone_id, period, zone_settings, stats):
    """MINUTE class on zones without 1m rollups: sampled estimates,
    visits ~ visitors (requests fallback), requests ~ page views."""
    now = timezone.now()
    minutes = MINUTE_PERIODS[period]
    since = now - timedelta(minutes=minutes)
    main_vars = {"zone": zone_id, "since": since.strftime(_TIME_FMT), "now": now.strftime(_TIME_FMT)}

    zone = _zone(_post_graphql(api_token, _ADAPTIVE_MAIN, main_vars, stats=stats))
    if zone is None:
        return None

    totals_groups = zone.get("totals") or []
    current = _estimate(totals_groups[0]) if totals_groups else {"requests": 0, "visits": 0, "bytes": 0}
    visitors_of = (lambda t: t["visits"]) if current["visits"] else (lambda t: t["requests"])

    previous = {"requests": 0, "visits": 0, "bytes": 0}
    prev_zone = _zone(_post_graphql(
        api_token,
        _ADAPTIVE_PREV,
        {
            "zone": zone_id,
            "since": (since - timedelta(minutes=minutes)).strftime(_TIME_FMT),
            "until": since.strftime(_TIME_FMT),
        },
        stats=stats,
    ))
    if prev_zone and prev_zone.get("previous"):
        previous = _estimate(prev_zone["previous"][0])

    chart = _skeleton("minute", now, minutes)
    by_key = {b["key"]: b for b in chart}
    for group in zone.get("series") or []:
        bucket = by_key.get((group.get("dimensions") or {}).get("datetimeMinute", ""))
        if bucket is None:
            continue
        est = _estimate(group)
        bucket["visitors"] = visitors_of(est)
        bucket["pageviews"] = est["requests"]
    for bucket in chart:
        bucket.pop("key", None)

    slices = _breakdown_slices(api_token, zone_id, since, now, stats)

    return {
        "overview": _overview(
            period,
            total_visitors=visitors_of(current),
            total_pageviews=current["requests"],
            visitor_growth_pct=_trend(visitors_of(current), visitors_of(previous)),
            pageviews_trend=_trend(current["requests"], previous["requests"]),
            breakdown_window_hours=max(1, minutes // 60),
            # No rollup maps on the adaptive path: flagged paths only.
            security={"flagged": slices["flagged"], "threats": 0, "threat_categories": []},
        ),
        "chart": chart,
        "pages": slices["pages"],
        "sources": [],
        "devices": {
            "devices": slices["devices"],
            "browsers": slices["browsers"],
            "operating_systems": slices["operating_systems"],
        },
        "countries": slices["countries"],
    }


def build_bundle(
    api_token: str,
    zone_id: str,
    *,
    period: str,
    zone_name: str = "",
    stats: dict | None = None,
) -> dict | None:
    """Six-slice dashboard bundle from zone analytics, or None on failure.

    zone_name is accepted for future paid-plan referrer support (self-
    referral exclusion); unused while clientRefererHost is inaccessible.
    """
    if not api_token or not zone_id:
        return None

    zone_settings = _zone_settings(api_token, zone_id, stats=stats)

    if period in MINUTE_PERIODS:
        # 1mGroups is disabled on free zones; minute-class periods use
        # adaptive estimates for every plan (a paid-zone 1m rollup path
        # can slot in here later, keyed on zone_settings["m1"]["enabled"]).
        return _build_from_adaptive(api_token, zone_id, period, zone_settings, stats)
    if period in HOURLY_PERIODS:
        return _build_from_rollups(api_token, zone_id, period, "h1", zone_settings, stats)

    period_key = period if period in DAILY_PERIODS else "30d"
    bundle = _build_from_rollups(api_token, zone_id, period_key, "d1", zone_settings, stats)
    if bundle is not None and period_key != period:
        bundle["overview"]["period"] = period
    return bundle
