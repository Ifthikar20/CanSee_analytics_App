"""GA4 Core reports the dashboard bundle does not carry (pages contract 4).

``countries``: total users by ISO country code (the bundle reports
country names, top ten only). ``ai_referrals``: sessions by source, per
day, landing page and country, with each source classified by the same
assistant lists the pixel uses. Both cache through snapshots for ten
minutes per website and property, and return None when GA4 failed with
no saved copy.

Rows come back raw ([code, value] / [key, assistant, sessions] lists);
the traffic services shape them into the API payloads.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.web_analytics.services import ga4_client, ga4_dashboard, snapshots

REPORT_TTL_SECONDS = 600


def window(period: str) -> tuple[str, str]:
    """(start, end) ISO dates, the window ga4_dashboard.build_bundle uses."""
    _dimension, days = ga4_dashboard.REPORT_PERIODS[period]
    today = timezone.now().date()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def assistant_of(source: str) -> str:
    """Canonical assistant key of a GA4 sessionSource, or ""."""
    from apps.analytics.services.event_ingestion_service import (
        _match_ai_referrer,
        _match_ai_utm_source,
    )

    return _match_ai_utm_source(source) or _match_ai_referrer(source)


def _country(value: str) -> str:
    code = (value or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""


def _cached(kind: str, integration, website, fetch) -> dict | None:
    property_id = (integration.metadata or {}).get("property_id") or ""
    payload = snapshots.get_or_fetch(
        f"{kind}:{property_id}", website.id, fetch, ttl=REPORT_TTL_SECONDS,
    )
    if not isinstance(payload, dict) or payload.get("pending"):
        return None
    return payload


def countries(integration, website, period: str) -> dict | None:
    """{"countries": [[code, users], ...], "unknown": users, "stale"};
    "(not set)" and blank countries count as unknown."""
    from apps.web_analytics.services.source_resolver import ga4_token

    def fetch():
        token = ga4_token(integration)
        if not token:
            return None
        report = ga4_client.run_report(
            token,
            (integration.metadata or {}).get("property_id"),
            metrics=["totalUsers"],
            dimensions=["countryId"],
            date_ranges=[window(period)],
            limit=250,
            order_by_metric="totalUsers",
            website_id=str(website.id),
        )
        if report is None:
            return None
        rows, unknown = [], 0
        for dims, mets in ga4_client._rows(report):
            if not mets:
                continue
            code = _country(dims[0] if dims else "")
            if code:
                rows.append([code, int(mets[0])])
            else:
                unknown += int(mets[0])
        return {"countries": rows, "unknown": unknown}

    return _cached(f"ga4cc:{period}", integration, website, fetch)


def ai_referrals(integration, website, period: str) -> dict | None:
    """Four reports cached together:
    {"start", "end", "total_sessions", "days": [[iso_day, assistant, n]],
     "landing": [[path, assistant, n]], "countries": [[code, assistant, n]], "stale"}.
    Only sessions whose source is an AI assistant are kept in the lists."""
    from apps.web_analytics.services.source_resolver import ga4_token

    def fetch():
        token = ga4_token(integration)
        if not token:
            return None
        property_id = (integration.metadata or {}).get("property_id")
        start, end = window(period)
        common = {"date_ranges": [(start, end)], "website_id": str(website.id)}

        def run(**kwargs):
            return ga4_client.run_report(
                token, property_id, metrics=["sessions"], **common, **kwargs,
            )

        totals = run()
        by_day = run(dimensions=["date", "sessionSource"], limit=5000)
        landing = run(dimensions=["landingPage", "sessionSource"], limit=2000,
                      order_by_metric="sessions")
        by_country = run(dimensions=["countryId", "sessionSource"], limit=2000)
        if totals is None or by_day is None or landing is None or by_country is None:
            return None

        total_rows = ga4_client._rows(totals)
        total_sessions = int(total_rows[0][1][0]) if total_rows and total_rows[0][1] else 0

        def classified(report, key_of):
            out = []
            for dims, mets in ga4_client._rows(report):
                if len(dims) < 2 or not mets:
                    continue
                assistant = assistant_of(dims[1])
                key = key_of(dims[0])
                if assistant and key:
                    out.append([key, assistant, int(mets[0])])
            return out

        def iso_day(value):
            value = (value or "").strip()
            if len(value) != 8 or not value.isdigit():
                return ""
            return f"{value[:4]}-{value[4:6]}-{value[6:8]}"

        def path(value):
            value = (value or "").strip()
            return value if value.startswith("/") else ""

        return {
            "start": start,
            "end": end,
            "total_sessions": total_sessions,
            "days": classified(by_day, iso_day),
            "landing": classified(landing, path),
            "countries": classified(by_country, _country),
        }

    return _cached(f"ga4ai:{period}", integration, website, fetch)
