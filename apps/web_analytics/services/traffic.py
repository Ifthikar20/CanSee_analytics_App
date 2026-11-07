"""Website traffic from the source the website has (pages contract 4b-4d).

One payload shape for three different sources, each labelled by what it
counts: the Cansee pixel (unique visitors, from our own tables), Google
Analytics 4 (users: the cached dashboard bundle plus a country report)
and Cloudflare (requests, bots included: a daily rollup report plus the
cached bundle's sampled breakdowns). A number a source cannot measure is
null, never 0, and a source that is missing, empty or failing is a 200
payload with an ``empty_reason``.

The source state is resolved before any cached copy is read, so a source
disconnected since its last fetch is never shown.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit

from apps.web_analytics.services import cloudflare_reports, ga4_reports, source_resolver
from apps.web_analytics.services.source_resolver import SOURCE_LABELS
from core.exceptions import CanseeException

PERIODS = ("7d", "30d", "90d")
SOURCES = ("auto", "pixel", "ga4", "cloudflare")
PERIOD_HOURS = {"7d": 7 * 24, "30d": 30 * 24, "90d": 90 * 24}
METRICS = {
    "pixel": ("visitors", "Unique visitors"),
    "ga4": ("users", "Users"),
    "cloudflare": ("requests", "Requests"),
}
# What each source can measure (contract 4b); Cloudflare's ai_crawlers is
# filled from the zone's probed capability.
MEASURES = {
    "pixel": {"visitors": "visitors", "countries": True, "referrers": True,
              "ai_referrals": True, "ai_crawlers": False},
    "ga4": {"visitors": "users", "countries": True, "referrers": True,
            "ai_referrals": True, "ai_crawlers": False},
    "ga_hosted": {"visitors": "live_only", "countries": False, "referrers": False,
                  "ai_referrals": False, "ai_crawlers": False},
    "cloudflare": {"visitors": "requests", "countries": True, "referrers": False,
                   "ai_referrals": False, "ai_crawlers": "unknown"},
}
MAX_REFERRERS = 12
MAX_PAGES = 10

NOTE_CF_REQUESTS = (
    "Cloudflare counts every request to your site, including bots and crawlers, "
    "so these numbers are not visitors."
)
# Countries come from the daily rollups, which are not sampled; only the
# adaptive page and device breakdowns are.
NOTE_CF_SAMPLED = (
    "Cloudflare samples its page and device breakdowns, so those are estimates."
)
NOTE_CF_BREAKDOWNS_FAILED = (
    "Cloudflare's page and device breakdowns could not be loaded just now."
)
NOTE_CF_REFERRERS = "Cloudflare does not report referrers on this plan."
NOTE_CF_BREAKDOWN_HOURS = (
    "Cloudflare's pages and devices cover the last {hours} hours on this plan."
)
NOTE_PIXEL_SINCE = "The Cansee pixel started recording on {date}; earlier days are not measured."
NOTE_STALE = "Showing the last saved copy; {source} could not be reached."


def invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def parse_period(value) -> str:
    period = value or "30d"
    if period not in PERIODS:
        raise invalid("period", "Choose a period of 7d, 30d or 90d.")
    return period


def parse_source(value, allowed: tuple = SOURCES) -> str:
    source = value or "auto"
    if source not in allowed:
        raise invalid("source", f"Choose a source: {', '.join(allowed)}.")
    return source


def change_pct(current: int, previous: int) -> float | None:
    # Nothing in the previous window means the change is unknown, not 0%.
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def share_pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def path_of(url: str) -> str:
    try:
        return urlsplit(url or "").path or "/"
    except ValueError:
        return "/"


def country_rows(pairs) -> list[dict]:
    """[(code, value)] -> every country with a value, descending, with its
    share of the known-country total."""
    merged: dict = {}
    for code, value in pairs:
        code = (code or "").strip().upper()
        if code:
            merged[code] = merged.get(code, 0) + int(value or 0)
    total = sum(merged.values())
    return [
        {"code": code, "value": value, "share_pct": share_pct(value, total)}
        for code, value in sorted(merged.items(), key=lambda item: (-item[1], item[0]))
        if value > 0
    ]


def top_pages(pairs) -> list[dict]:
    """[(url or path, views)] -> the top paths, one row per path."""
    merged: dict = {}
    for url, views in pairs:
        path = path_of(url)
        merged[path] = merged.get(path, 0) + int(views or 0)
    ranked = sorted(merged.items(), key=lambda item: (-item[1], item[0]))[:MAX_PAGES]
    return [{"path": path, "views": views} for path, views in ranked]


def _devices(rows) -> list[dict]:
    return [
        {"name": row.get("name") or "Unknown", "value": int(row.get("count") or 0),
         "share_pct": row.get("pct")}
        for row in rows or []
    ]


def _referrers(rows) -> list[dict]:
    return [
        {
            "source": row.get("source") or "",
            "medium": row.get("medium") or "",
            "sessions": int(row.get("sessions") or row.get("count") or 0),
            "is_ai": row.get("medium") == "ai",
        }
        for row in (rows or [])[:MAX_REFERRERS]
    ]


# -- Sources --------------------------------------------------------------------


def traffic_sources(website) -> dict:
    """TrafficSources (contract 4b)."""
    integrations = source_resolver.integrations_for(website)
    states = source_resolver.source_states(website, integrations=integrations)
    sources = []
    for key in ("pixel", "ga4", "ga_hosted", "cloudflare"):
        state = states[key]
        measures = dict(MEASURES[key])
        if key == "cloudflare":
            measures["ai_crawlers"] = state["ai_crawlers"]
        sources.append({
            "key": key,
            "label": SOURCE_LABELS[key],
            "connected": state["connected"],
            "has_data": state["has_data"],
            "status": state["status"],
            "detail": state["detail"],
            "measured_since": state["measured_since"],
            "measures": measures,
        })
    return {
        "default_source": source_resolver.auto_source(website, states=states),
        "sources": sources,
    }


def _available(states: dict) -> list[str]:
    out = ["pixel"] if states["pixel"]["connected"] else []
    return out + [key for key in ("ga4", "cloudflare") if states[key]["status"] == "ok"]


def _resolve(website, requested: str) -> tuple:
    """(source, empty_reason, integration, states) for a request."""
    integrations = source_resolver.integrations_for(website)
    states = source_resolver.source_states(website, integrations=integrations)
    if requested == "auto":
        source = source_resolver.auto_source(website, states=states)
    else:
        source = requested
    if source is None:
        return None, "no_source", None, states
    if source == "pixel":
        if states["pixel"]["has_data"]:
            return source, None, None, states
        reason = "no_data" if states["pixel"]["connected"] else "not_connected"
        return source, reason, None, states
    if states[source]["status"] != "ok":
        return source, "not_connected", None, states
    return source, None, integrations[source], states


def _head(source, period: str, states: dict) -> dict:
    metric, metric_label = METRICS.get(source, (None, None))
    return {
        "source": source,
        "source_label": SOURCE_LABELS.get(source) if source else None,
        "period": period,
        "available_sources": _available(states),
        "metric": metric,
        "metric_label": metric_label,
        "sampled": source == "cloudflare",
        "stale": False,
    }


def _pixel_window(period: str):
    from core.utils.date_utils import get_date_range

    return get_date_range(period)


def _pixel_since_note(measured_since: str | None, window_start: date) -> list[str]:
    if measured_since and measured_since > window_start.isoformat():
        return [NOTE_PIXEL_SINCE.format(date=measured_since)]
    return []


# -- Traffic (4c) ----------------------------------------------------------------


def _pixel(website, period: str, measured_since: str | None) -> dict:
    from apps.analytics.models import PageEvent, Session, Visitor
    from apps.analytics.services.analytics_service import AnalyticsService
    from apps.analytics.services.daily_stats import DailyStatsService

    website_id = str(website.id)
    start, end = _pixel_window(period)
    prev_start = start - (end - start)
    since = date.fromisoformat(measured_since) if measured_since else None
    # A change needs the whole previous window measured by the pixel.
    comparable = since is not None and since <= prev_start.date()

    overview = AnalyticsService.get_overview(website_id=website_id, period=period)
    pageviews_prev = PageEvent.objects.filter(
        website_id=website_id, event_type="pageview", timestamp__range=(prev_start, start),
    ).count()
    sessions = Session.objects.filter(visitor__website_id=website_id)
    sessions_now = sessions.filter(started_at__range=(start, end)).count()
    sessions_prev = sessions.filter(started_at__range=(prev_start, start)).count()

    chart = DailyStatsService.get_chart_data(website_id=website_id, period=period)
    measured = [bool(measured_since) and row["date"] >= measured_since for row in chart]

    def column(name: str) -> list:
        return [row[name] if ok else None for row, ok in zip(chart, measured, strict=True)]

    countries = DailyStatsService.get_country_breakdown(
        website_id=website_id, period=period, limit=250,
    )
    unknown = Visitor.objects.filter(
        website_id=website_id, last_seen__range=(start, end), geo_country="",
    ).count()
    devices = DailyStatsService.get_device_breakdown(website_id=website_id, period=period)
    pages = AnalyticsService.get_top_pages(website_id=website_id, period=period, limit=50)
    return {
        "measured_since": measured_since,
        "overview": {
            "visitors": overview["total_visitors"],
            "visitors_change_pct": overview["visitor_growth_pct"] if comparable else None,
            "pageviews": overview["total_pageviews"],
            "pageviews_change_pct": (
                change_pct(overview["total_pageviews"], pageviews_prev) if comparable else None
            ),
            "sessions": sessions_now,
            "sessions_change_pct": change_pct(sessions_now, sessions_prev) if comparable else None,
        },
        "series": {
            "labels": [row["date"] for row in chart],
            "visitors": column("visitors"),
            "pageviews": column("pageviews"),
            "sessions": column("sessions"),
        },
        "countries": country_rows((row["name"], row["visitors"]) for row in countries),
        "countries_unknown": unknown,
        "referrers": _referrers(
            AnalyticsService.get_traffic_sources(website_id=website_id, period=period)
        ),
        "pages": top_pages((row["url"], row["views"]) for row in pages),
        "devices": _devices(devices.get("devices")),
        "breakdown_hours": None,
        "notes": _pixel_since_note(measured_since, start.date()),
    }


def _ga4(website, integration, period: str) -> dict | str:
    bundle = source_resolver.fetch_bundle(website, "ga4", period, integration=integration)
    if bundle is None:
        return "source_error"
    overview = bundle.get("overview") or {}
    chart = bundle.get("chart") or []
    visitors = int(overview.get("total_visitors") or 0)
    pageviews = int(overview.get("total_pageviews") or 0)
    sessions = sum(int(bucket.get("sessions") or 0) for bucket in chart)
    if not (visitors or pageviews or sessions):
        return "no_data"
    report = ga4_reports.countries(integration, website, period)
    return {
        "stale": bool(bundle.get("stale")) or bool(report and report.get("stale")),
        "measured_since": None,
        "overview": {
            "visitors": visitors,
            "visitors_change_pct": overview.get("visitor_growth_pct"),
            "pageviews": pageviews,
            "pageviews_change_pct": overview.get("pageviews_trend"),
            "sessions": sessions,
            # The bundle has no session-count trend.
            "sessions_change_pct": None,
        },
        "series": {
            "labels": [bucket.get("date") for bucket in chart],
            "visitors": [int(bucket.get("visitors") or 0) for bucket in chart],
            "pageviews": [int(bucket.get("pageviews") or 0) for bucket in chart],
            "sessions": [int(bucket.get("sessions") or 0) for bucket in chart],
        },
        "countries": country_rows(report["countries"]) if report else [],
        "countries_unknown": report["unknown"] if report else None,
        "referrers": _referrers(bundle.get("sources")),
        "pages": top_pages((row.get("url"), row.get("views")) for row in bundle.get("pages") or []),
        "devices": _devices((bundle.get("devices") or {}).get("devices")),
        "breakdown_hours": None,
        "notes": [],
    }


def _cloudflare(website, integration, period: str) -> dict | str:
    report = cloudflare_reports.report(integration, website, period)
    if report is None:
        return "source_error"
    totals, previous = report["totals"], report["previous"]
    if not totals["requests"]:
        return "no_data"
    # Pages and devices come from the dashboard bundle's sampled adaptive
    # breakdowns, which the zone's plan may cap to a shorter window. With
    # no bundle, or one whose breakdown query failed, they were not
    # measured: null with a note, never an empty list. A bundle cached
    # before breakdowns_failed existed reads as not failed.
    bundle = source_resolver.fetch_bundle(website, "cloudflare", period, integration=integration)
    measured = bundle is not None and not bundle.get("breakdowns_failed")
    hours = None
    if measured:
        notes = [NOTE_CF_REQUESTS, NOTE_CF_SAMPLED, NOTE_CF_REFERRERS]
        window = (bundle.get("overview") or {}).get("breakdown_window_hours")
        if window and window < PERIOD_HOURS[period]:
            hours = window
            notes.append(NOTE_CF_BREAKDOWN_HOURS.format(hours=hours))
    else:
        notes = [NOTE_CF_REQUESTS, NOTE_CF_BREAKDOWNS_FAILED, NOTE_CF_REFERRERS]
    return {
        "stale": bool(report.get("stale")) or bool(bundle and bundle.get("stale")),
        "measured_since": None,
        "overview": {
            "visitors": totals["requests"],
            "visitors_change_pct": change_pct(totals["requests"], previous["requests"]),
            "pageviews": totals["pageviews"],
            "pageviews_change_pct": change_pct(totals["pageviews"], previous["pageviews"]),
            # No session model at the edge.
            "sessions": None,
            "sessions_change_pct": None,
        },
        "series": {
            "labels": list(report["labels"]),
            "visitors": list(report["requests"]),
            "pageviews": list(report["pageviews"]),
            "sessions": [None] * len(report["labels"]),
        },
        "countries": country_rows(report["countries"]),
        "countries_unknown": report["unknown"],
        "referrers": None,
        "pages": top_pages(
            (row.get("url"), row.get("views")) for row in bundle.get("pages") or []
        ) if measured else None,
        "devices": _devices((bundle.get("devices") or {}).get("devices")) if measured else None,
        "breakdown_hours": hours,
        "notes": notes,
    }


def traffic(website, *, period: str, source: str) -> dict:
    """Traffic (contract 4c) for ``source`` (auto, pixel, ga4, cloudflare)."""
    source, reason, integration, states = _resolve(website, source)
    body = None
    if reason is None:
        if source == "pixel":
            body = _pixel(website, period, states["pixel"]["measured_since"])
        elif source == "ga4":
            body = _ga4(website, integration, period)
        else:
            body = _cloudflare(website, integration, period)
        if isinstance(body, str):
            reason, body = body, None

    payload = _head(source, period, states)
    if body is None:
        return {
            **payload,
            "measured_since": None,
            "empty_reason": reason,
            "overview": None,
            "series": None,
            "countries": [],
            "countries_unknown": None,
            "referrers": [],
            "pages": [],
            "devices": [],
            "breakdown_hours": None,
            "notes": [],
        }
    notes = list(body.pop("notes"))
    payload["stale"] = bool(body.pop("stale", False))
    if payload["stale"]:
        notes.append(NOTE_STALE.format(source=payload["source_label"]))
    return {**payload, "measured_since": body.pop("measured_since"), "empty_reason": None,
            **body, "notes": notes}


# -- Countries (4d) --------------------------------------------------------------


def _country_part(website, source: str, integration, period: str, states: dict) -> dict | str:
    if source == "pixel":
        from apps.analytics.models import Visitor
        from apps.analytics.services.daily_stats import DailyStatsService

        start, end = _pixel_window(period)
        rows = DailyStatsService.get_country_breakdown(
            website_id=str(website.id), period=period, limit=250,
        )
        unknown = Visitor.objects.filter(
            website_id=website.id, last_seen__range=(start, end), geo_country="",
        ).count()
        return {
            "pairs": [(row["name"], row["visitors"]) for row in rows],
            "unknown": unknown,
            "stale": False,
            "notes": _pixel_since_note(states["pixel"]["measured_since"], start.date()),
        }
    reports = ga4_reports if source == "ga4" else cloudflare_reports
    found = reports.countries(integration, website, period)
    if found is None:
        return "source_error"
    if not found["countries"] and not found["unknown"]:
        return "no_data"
    return {
        "pairs": found["countries"],
        "unknown": found["unknown"],
        "stale": bool(found.get("stale")),
        # Rollup countries are exact, so only the requests note applies.
        "notes": [NOTE_CF_REQUESTS] if source == "cloudflare" else [],
    }


def traffic_countries(website, *, period: str, source: str) -> dict:
    """TrafficCountries (contract 4d): the country part of 4c, from one
    upstream report."""
    source, reason, integration, states = _resolve(website, source)
    payload = _head(source, period, states)
    part = None
    if reason is None:
        part = _country_part(website, source, integration, period, states)
        if isinstance(part, str):
            reason, part = part, None
    if part is None:
        return {**payload, "empty_reason": reason, "countries": [], "countries_unknown": None,
                "total": None, "notes": []}

    countries = country_rows(part["pairs"])
    notes = list(part["notes"])
    payload["stale"] = part["stale"]
    if part["stale"]:
        notes.append(NOTE_STALE.format(source=payload["source_label"]))
    return {
        **payload,
        "empty_reason": None,
        "countries": countries,
        "countries_unknown": part["unknown"],
        "total": sum(row["value"] for row in countries) + part["unknown"],
        "notes": notes,
    }
