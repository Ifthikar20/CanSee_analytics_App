"""Visits from AI assistants and AI crawler requests (pages contract 4e).

Two different measurements on one page:

- Referrals: sessions that arrived from an AI assistant, from the Cansee
  pixel (``Session.medium == "ai"``, classified at ingestion) or from GA4
  (``sessionSource`` classified with the same assistant lists).
- Crawlers: requests whose user agent is a known AI crawler, from
  Cloudflare only (services/ai_crawlers.py); crawlers run no JavaScript,
  so the pixel and GA4 never see them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from apps.web_analytics.services import ai_crawlers, ga4_reports, source_resolver
from apps.web_analytics.services.source_resolver import SOURCE_LABELS
from apps.web_analytics.services.traffic import (
    NOTE_PIXEL_SINCE,
    NOTE_STALE,
    path_of,
    share_pct,
)

SOURCES = ("auto", "pixel", "ga4")
MAX_LANDING_PAGES = 10
# The pixel's canonical assistant names (event_ingestion_service
# _AI_UTM_SOURCES / _AI_REFERRER_HOSTS) and their labels.
ASSISTANT_LABELS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "copilot": "Copilot",
    "meta-ai": "Meta AI",
    "poe": "Poe",
    "you": "You.com",
}


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _unavailable(source, reason: str) -> dict:
    return {
        "source": source,
        "source_label": SOURCE_LABELS.get(source) if source else None,
        "available": False,
        "reason": reason,
        "total_sessions": None,
        "ai_sessions": None,
        "ai_share_pct": None,
        "assistants": [],
        "series": {"labels": [], "by_assistant": {}},
        "landing_pages": [],
        "countries": [],
        "measured_since": None,
        "notes": [],
    }


def _shape(source: str, *, labels: list[str], total_sessions: int, days: list, landing: list,
           countries: list, measured_since: str | None, stale: bool) -> dict:
    """The referrals payload from classified rows: days [iso, assistant, n],
    landing [url or path, assistant, n], countries [code, assistant, n]."""
    totals: Counter = Counter()
    per_day: dict = defaultdict(Counter)
    for day, assistant, sessions in days:
        if assistant:
            totals[assistant] += sessions
            per_day[day][assistant] += sessions
    ai_sessions = sum(totals.values())
    order = sorted((key for key in totals if totals[key] > 0), key=lambda key: (-totals[key], key))

    def measured(day: str) -> bool:
        return measured_since is None or day >= measured_since

    pages: dict = defaultdict(Counter)
    for url, assistant, sessions in landing:
        if assistant:
            pages[path_of(url)][assistant] += sessions
    ranked = sorted(pages.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    by_country: Counter = Counter()
    for code, assistant, sessions in countries:
        code = (code or "").strip().upper()
        if code and assistant:
            by_country[code] += sessions

    notes = []
    if measured_since and labels and measured_since > labels[0]:
        notes.append(NOTE_PIXEL_SINCE.format(date=measured_since))
    if stale:
        notes.append(NOTE_STALE.format(source=SOURCE_LABELS[source]))
    return {
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "available": True,
        "reason": None,
        "total_sessions": total_sessions,
        "ai_sessions": ai_sessions,
        "ai_share_pct": share_pct(ai_sessions, total_sessions),
        "assistants": [
            {
                "key": key,
                "label": ASSISTANT_LABELS.get(key, key.title()),
                "sessions": totals[key],
                "share_pct": share_pct(totals[key], ai_sessions),
            }
            for key in order
        ],
        "series": {
            "labels": labels,
            "by_assistant": {
                key: [per_day[day][key] if measured(day) else None for day in labels]
                for key in order
            },
        },
        "landing_pages": [
            {
                "path": path,
                "sessions": sum(counts.values()),
                "by_assistant": {key: counts[key] for key in order if counts[key]},
            }
            for path, counts in ranked[:MAX_LANDING_PAGES]
        ],
        "countries": [
            {"code": code, "sessions": sessions}
            for code, sessions in sorted(by_country.items(), key=lambda item: (-item[1], item[0]))
            if sessions > 0
        ],
        "measured_since": measured_since,
        "notes": notes,
    }


def _pixel_referrals(website, period: str, measured_since: str | None) -> dict:
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    from apps.analytics.models import Session
    from core.utils.date_utils import get_date_range

    start, end = get_date_range(period)
    base = Session.objects.filter(visitor__website_id=website.id, started_at__range=(start, end))
    ai = base.filter(medium="ai")
    per_day = ai.annotate(day=TruncDate("started_at")).values("day", "source")
    days = [
        [row["day"].isoformat(), row["source"], row["n"]]
        for row in per_day.annotate(n=Count("id"))
    ]
    landing = [
        [row["entry_page"], row["source"], row["n"]]
        for row in ai.values("entry_page", "source").annotate(n=Count("id"))
    ]
    countries = [
        [row["visitor__geo_country"], row["source"], row["n"]]
        for row in ai.values("visitor__geo_country", "source").annotate(n=Count("id"))
    ]
    return _shape(
        "pixel", labels=_days(start.date(), end.date()), total_sessions=base.count(), days=days,
        landing=landing, countries=countries, measured_since=measured_since, stale=False,
    )


def _referral_source(requested: str, states: dict) -> str | None:
    if requested != "auto":
        return requested
    if states["pixel"]["has_data"]:
        return "pixel"
    if states["ga4"]["status"] == "ok":
        return "ga4"
    if states["pixel"]["connected"]:
        return "pixel"
    return None


def _referrals(website, period: str, requested: str, states: dict, integrations: dict) -> dict:
    source = _referral_source(requested, states)
    if source is None:
        return _unavailable(None, "no_source")
    if source == "pixel":
        pixel = states["pixel"]
        if not pixel["has_data"]:
            return _unavailable("pixel", "no_data" if pixel["connected"] else "not_connected")
        return _pixel_referrals(website, period, pixel["measured_since"])
    if states["ga4"]["status"] != "ok":
        return _unavailable("ga4", "not_connected")
    found = ga4_reports.ai_referrals(integrations["ga4"], website, period)
    if found is None:
        return _unavailable("ga4", "source_error")
    if not found["total_sessions"]:
        return _unavailable("ga4", "no_data")
    return _shape(
        "ga4", labels=_days(date.fromisoformat(found["start"]), date.fromisoformat(found["end"])),
        total_sessions=found["total_sessions"], days=found["days"], landing=found["landing"],
        countries=found["countries"], measured_since=None, stale=bool(found.get("stale")),
    )


def _crawlers(website, states: dict, integrations: dict) -> dict:
    status = states["cloudflare"]["status"]
    if status == "disabled":
        return ai_crawlers.unavailable("disabled")
    if status != "ok":
        return ai_crawlers.unavailable("no_cloudflare")
    return ai_crawlers.crawler_report(integrations["cloudflare"], website)


def ai_traffic(website, *, period: str, source: str) -> dict:
    """AiTraffic (contract 4e); ``source`` picks the referral source (auto,
    pixel, ga4). Crawlers always cover Cloudflare's last 7 days."""
    integrations = source_resolver.integrations_for(website)
    states = source_resolver.source_states(website, integrations=integrations)
    return {
        "period": period,
        "referrals": _referrals(website, period, source, states, integrations),
        "crawlers": _crawlers(website, states, integrations),
    }
