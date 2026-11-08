"""
GA4-backed dashboard bundle: the six Overview payloads (overview, chart,
pages, sources, devices, countries) built from the client's own GA4
property, mirroring the pixel endpoints' exact shapes
(apps/analytics/services/analytics_service.py + daily_stats.py, consumed
by ftb-ui/src/stores/analytics.js:79-113).

Used by source_resolver when the pixel has no data but GA4 is connected —
"if Google, then we read from Google." Two regimes per period:

  - Sub-hour periods (5m..1h, and 6h best-effort): served from the GA4
    Realtime API snapshot (30-minute window is a hard Google cap).
    Realtime has no source/medium, so `sources` stays empty and AI
    attribution remains a pixel/Core-report feature.
  - Day+ periods (24h, 7d..6mo): Core Data API (runReport), including a
    two-range KPI call for previous-period trends, and AI-referrer
    classification of sessionSource using the same list as the pixel
    ingestion pipeline.

Metric mapping is honest, not cosmetic: totalUsers -> unique visitors,
screenPageViews -> page views, averageSessionDuration -> avg session
("M:SS" string like the pixel), GA4 bounceRate (1 - engagementRate,
fraction) -> bounce %. Trends are None when the previous period is empty.
Chart windows are zero-filled like DailyStatsService.get_chart_data, so
the frontend's noData heuristic behaves identically.

Everything degrades to empty slices, never raises; one bundle is built
per (website, period) and cached by the resolver.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.web_analytics.services import ga4_client

logger = logging.getLogger("apps")

# period -> (dimension, days_back). 24h uses hour buckets; longer
# periods use daily buckets.
REPORT_PERIODS = {
    "24h": ("dateHour", 1),
    "7d": ("date", 7),
    "30d": ("date", 30),
    "90d": ("date", 90),
    "6mo": ("date", 182),
}

# Sub-day periods come from the realtime snapshot; values are the wanted
# window in minutes (capped at Google's 30-minute realtime window).
# "5m" stays as a harmless alias for sessions that persisted it before
# the UI's shortest button became 10m.
REALTIME_PERIODS = {"5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 30, "6h": 30}

# Same palette contract the pixel devices endpoint ships
# (apps/analytics/services/daily_stats.py:92-96).
DEVICE_COLORS = {
    "desktop": "var(--brand-accent)",
    "mobile": "var(--text-primary)",
    "tablet": "var(--text-muted)",
}

SOURCE_NAME = "ga4"


def _empty_bundle(period: str) -> dict:
    return {
        "overview": _overview(period),
        "chart": [],
        "pages": [],
        "sources": [],
        "devices": {"devices": [], "browsers": [], "operating_systems": []},
        "countries": [],
    }


def _overview(period: str, **values) -> dict:
    base = {
        "period": period,
        "total_visitors": 0,
        "total_pageviews": 0,
        "visitor_growth_pct": None,
        "pageviews_trend": None,
        "avg_session": None,
        "session_trend": None,
        "bounce_rate": None,
        "bounce_trend": None,
        "realtime": 0,
        "data_source": SOURCE_NAME,
    }
    base.update(values)
    return base


def _trend(current: float, previous: float) -> float | None:
    # Nothing in the previous window means the change is unknown, not 0%.
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _pct(part: float, total: float) -> float:
    if not total:
        return 0.0
    return round(part / total * 100, 1)


def _mmss(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _ai_medium(source: str, medium: str) -> str:
    """Mirror the pixel pipeline's AI-referrer classification for GA4
    sessionSource values (chatgpt.com, perplexity.ai, ...)."""
    from apps.analytics.services.event_ingestion_service import _match_ai_referrer

    if _match_ai_referrer(source):
        return "ai"
    return medium or "none"


def _named_counts(report, *, limit: int | None = None) -> list[dict]:
    """[(name, users)] rows -> [{name, count, pct}] shares."""
    rows = [
        (dims[0], int(mets[0]))
        for dims, mets in ga4_client._rows(report)
        if dims and mets and dims[0] not in ("", "(not set)")
    ]
    if limit:
        rows = rows[:limit]
    total = sum(count for _name, count in rows) or 1
    return [
        {"name": name, "count": count, "pct": _pct(count, total)}
        for name, count in rows
    ]


def _zero_filled_chart(dimension: str, start, end) -> list[dict]:
    """Full bucket skeleton, mirroring DailyStatsService's zero-fill."""
    buckets = []
    if dimension == "dateHour":
        cursor = end.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        for i in range(24):
            slot = cursor + timedelta(hours=i)
            buckets.append({
                "key": slot.strftime("%Y%m%d%H"),
                "date": slot.date().isoformat(),
                "label": f"{slot.hour:02d}:00",
                "visitors": 0, "pageviews": 0, "sessions": 0,
            })
    else:
        day = start
        while day <= end:
            buckets.append({
                "key": day.strftime("%Y%m%d"),
                "date": day.isoformat(),
                "label": f"{day:%b} {day.day}",
                "visitors": 0, "pageviews": 0, "sessions": 0,
            })
            day += timedelta(days=1)
    return buckets


def _from_realtime(snapshot: dict | None, window_minutes: int, period: str) -> dict:
    """Derive the bundle from a realtime snapshot (sub-hour periods)."""
    if not snapshot or snapshot.get("pending"):
        return _empty_bundle(period)

    series = snapshot.get("per_minute") or []
    if window_minutes < len(series):
        series = series[-window_minutes:]
    chart = [
        {
            "label": "now" if row["minutes_ago"] == 0 else f"-{row['minutes_ago']}m",
            "visitors": row.get("active_users", 0),
            "pageviews": row.get("page_views", 0),
            "sessions": 0,
        }
        for row in series
    ]

    active_total = snapshot.get("active_users") or 0
    devices = [
        {
            "name": (d["device"] or "Unknown").capitalize(),
            "count": d["active_users"],
            "pct": _pct(d["active_users"], active_total),
            "color": DEVICE_COLORS.get((d["device"] or "").lower(), "var(--text-muted)"),
        }
        for d in snapshot.get("devices") or []
    ]
    countries = [
        {
            "name": c["country"],
            "pct": _pct(c["active_users"], active_total),
            "visitors": c["active_users"],
        }
        for c in snapshot.get("countries") or []
    ]

    return {
        "overview": _overview(
            period,
            total_visitors=snapshot.get("active_users", 0),
            total_pageviews=snapshot.get("page_views", 0),
            realtime=snapshot.get("active_users", 0),
        ),
        "chart": chart,
        "pages": [
            {"url": p["page"], "views": p["page_views"]}
            for p in snapshot.get("top_pages") or []
        ],
        # GA4 realtime carries no source/medium at all.
        "sources": [],
        "devices": {"devices": devices, "browsers": [], "operating_systems": []},
        "countries": countries,
    }


def build_bundle(access_token: str, property_id: str, *, period: str, website_id=None) -> dict | None:
    """Build the six-slice dashboard bundle for one period.

    Returns None only when the KPI call fails outright (caller serves a
    stale copy or falls back to the pixel path); individual missing
    breakdowns degrade to empty slices.
    """
    if period in REALTIME_PERIODS:
        snapshot = ga4_client.build_realtime_snapshot(
            access_token, property_id, website_id=website_id
        )
        if snapshot is None:
            return None
        return _from_realtime(snapshot, REALTIME_PERIODS[period], period)

    dimension, days = REPORT_PERIODS.get(period, ("date", 30))
    today = timezone.now().date()
    start = today - timedelta(days=days - 1) if dimension == "date" else today - timedelta(days=1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=(today - start).days)
    current = (start.isoformat(), today.isoformat())
    previous = (prev_start.isoformat(), prev_end.isoformat())

    common = {"website_id": website_id}
    kpis = ga4_client.run_report(
        access_token,
        property_id,
        metrics=["totalUsers", "screenPageViews", "averageSessionDuration", "bounceRate"],
        date_ranges=[current, previous],
        **common,
    )
    if kpis is None:
        return None

    # Two dateRanges make Google add an implicit dateRange dimension.
    totals = {"date_range_0": [0, 0, 0.0, 0.0], "date_range_1": [0, 0, 0.0, 0.0]}
    for dims, mets in ga4_client._rows(kpis):
        key = dims[0] if dims else "date_range_0"
        totals[key] = (mets + [0, 0, 0, 0])[:4]
    cur, prev = totals["date_range_0"], totals["date_range_1"]

    chart_report = ga4_client.run_report(
        access_token, property_id,
        metrics=["totalUsers", "screenPageViews", "sessions"],
        dimensions=[dimension],
        date_ranges=[current],
        limit=400,
        **common,
    )
    now = timezone.now()
    chart = _zero_filled_chart(dimension, start, today if dimension == "date" else now)
    by_key = {b["key"]: b for b in chart}
    for dims, mets in ga4_client._rows(chart_report):
        bucket = by_key.get(dims[0] if dims else "")
        if bucket is None:
            continue
        bucket["visitors"] = int(mets[0]) if mets else 0
        bucket["pageviews"] = int(mets[1]) if len(mets) > 1 else 0
        bucket["sessions"] = int(mets[2]) if len(mets) > 2 else 0
    for bucket in chart:
        bucket.pop("key", None)

    pages_report = ga4_client.run_report(
        access_token, property_id,
        metrics=["screenPageViews"],
        dimensions=["pagePath"],
        date_ranges=[current],
        limit=10,
        order_by_metric="screenPageViews",
        **common,
    )
    pages = [
        {"url": dims[0], "views": int(mets[0])}
        for dims, mets in ga4_client._rows(pages_report)
        if dims
    ]

    sources_report = ga4_client.run_report(
        access_token, property_id,
        metrics=["sessions"],
        dimensions=["sessionSource", "sessionMedium"],
        date_ranges=[current],
        limit=12,
        order_by_metric="sessions",
        **common,
    )
    source_rows = [
        (dims[0] or "direct", dims[1] if len(dims) > 1 else "", int(mets[0]))
        for dims, mets in ga4_client._rows(sources_report)
        if mets
    ]
    total_sessions = sum(sessions for _s, _m, sessions in source_rows)
    sources = [
        {
            "source": name,
            "medium": _ai_medium(name, medium),
            "count": sessions,
            "sessions": sessions,
            "percentage": _pct(sessions, total_sessions),
        }
        for name, medium, sessions in source_rows
    ]

    breakdown = {}
    for key, dim, limit in (
        ("devices", "deviceCategory", None),
        ("browsers", "browser", 8),
        ("operating_systems", "operatingSystem", 8),
    ):
        report = ga4_client.run_report(
            access_token, property_id,
            metrics=["totalUsers"],
            dimensions=[dim],
            date_ranges=[current],
            limit=limit or 8,
            order_by_metric="totalUsers",
            **common,
        )
        breakdown[key] = _named_counts(report, limit=limit)
    for item in breakdown["devices"]:
        raw = item["name"].lower()
        item["name"] = item["name"].capitalize()
        item["color"] = DEVICE_COLORS.get(raw, "var(--text-muted)")

    countries_report = ga4_client.run_report(
        access_token, property_id,
        metrics=["totalUsers"],
        dimensions=["country"],
        date_ranges=[current],
        limit=10,
        order_by_metric="totalUsers",
        **common,
    )
    countries = [
        {"name": row["name"], "pct": row["pct"], "visitors": row["count"]}
        for row in _named_counts(countries_report, limit=10)
    ]

    return {
        "overview": _overview(
            period,
            total_visitors=int(cur[0]),
            total_pageviews=int(cur[1]),
            visitor_growth_pct=_trend(cur[0], prev[0]),
            pageviews_trend=_trend(cur[1], prev[1]),
            avg_session=_mmss(cur[2]),
            session_trend=_trend(cur[2], prev[2]),
            # GA4 bounceRate is a 0..1 fraction (1 - engagementRate).
            bounce_rate=round(cur[3] * 100, 1),
            bounce_trend=_trend(cur[3], prev[3]),
        ),
        "chart": chart,
        "pages": pages,
        "sources": sources,
        "devices": breakdown,
        "countries": countries,
    }
