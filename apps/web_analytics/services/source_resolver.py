"""
Dashboard data-source resolver: "the dashboard shows whichever source
the website actually has."

Precedence per website:
  1. Pixel — if ANY pixel event exists, the pixel pipeline serves and
     this module stays out of the way entirely (richest data, incl. AI
     attribution). Checked cheaply and cached for a minute.
  2. GA4 — connected property, served via ga4_dashboard.
  3. Cloudflare — connected zone, served via cloudflare_dashboard.
  4. None — the pixel views return their normal zeros.

The six Overview endpoints in apps/analytics call dashboard_slice() at
the top of their handlers; a None return means "proceed with the pixel
path". One bundle is built per (source, website, period) and cached
read-through (snapshots.get_or_fetch), so the frontend's six parallel
requests cost at most one upstream build.

The traffic pages (services/traffic.py, services/ai_traffic.py) use the
same precedence through source_states() / auto_source(), and read the
same cached bundles through fetch_bundle().
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max, Min

from apps.web_analytics.services import (
    ai_crawlers,
    cloudflare_dashboard,
    ga4_dashboard,
    snapshots,
)

logger = logging.getLogger("apps")

PIXEL_CHECK_TTL_SECONDS = 60
PIXEL_SPAN_TTL_SECONDS = 60

SLICES = ("overview", "chart", "pages", "sources", "devices", "countries")

SOURCE_LABELS = {
    "pixel": "Cansee pixel",
    "ga4": "Google Analytics 4",
    "ga_hosted": "Cansee Google tag",
    "cloudflare": "Cloudflare",
}
# Integration.type behind each external source key.
_INTEGRATION_TYPES = {"ga4": "ga", "ga_hosted": "ga_hosted", "cloudflare": "cloudflare"}


def _feature_enabled() -> bool:
    return bool(getattr(settings, "WEB_ANALYTICS_ENABLED", True))


def _pixel_has_data(website_id) -> bool:
    """True when the website has ever ingested a pixel event (cached)."""
    key = f"wa:haspixel:{website_id}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return bool(cached)

    from apps.analytics.models import PageEvent

    has_data = PageEvent.objects.filter(website_id=website_id).exists()
    try:
        # Once the pixel starts sending, that answer is stable — cache
        # the positive longer than the negative.
        cache.set(key, has_data, 3600 if has_data else PIXEL_CHECK_TTL_SECONDS)
    except Exception:
        pass
    return has_data


def _active_source(website):
    """('ga4'|'cloudflare', Integration) for the best connected source,
    or (None, None)."""
    from apps.websites.models import Integration

    integrations = {
        i.type: i
        for i in Integration.objects.filter(
            website=website, type__in=("ga", "cloudflare"), is_active=True
        )
    }

    ga = integrations.get("ga")
    if ga is not None and (ga.metadata or {}).get("property_id") and (
        ga.access_token or ga.refresh_token
    ):
        return "ga4", ga

    cf = integrations.get("cloudflare")
    if cf is not None and cf.access_token and (cf.metadata or {}).get("zone_id"):
        return "cloudflare", cf

    return None, None


def ga4_token(integration) -> str:
    """A usable access token for a ga integration, refreshed inline when
    it is near expiry; "" when the integration is inactive or has none."""
    if integration.needs_token_refresh():
        from apps.web_analytics.services import ga4_oauth

        try:
            ga4_oauth.refresh_access_token(integration)
        except Exception as exc:
            logger.warning("GA4 inline token refresh failed: %s", exc)
    if not integration.is_active or not integration.access_token:
        return ""
    return integration.access_token


def _fetch_ga4(website, integration, period):
    token = ga4_token(integration)
    if not token:
        return None
    return ga4_dashboard.build_bundle(
        token,
        (integration.metadata or {})["property_id"],
        period=period,
        website_id=str(website.id),
    )


def _fetch_cloudflare(website, integration, period):
    metadata = integration.metadata or {}
    return cloudflare_dashboard.build_bundle(
        integration.access_token,
        metadata["zone_id"],
        period=period,
        zone_name=metadata.get("zone_name") or "",
    )


def fetch_bundle(website, source: str, period: str, *, integration=None) -> dict | None:
    """The cached GA4 or Cloudflare dashboard bundle for one period (the
    snapshot dashboard_slice serves), or None while it is pending or when
    the upstream build failed with no saved copy. A saved copy served
    after a failure carries stale=True.

    ``integration`` is the source's usable Integration; it is looked up
    when omitted (None when the source is not usable now).
    """
    if source not in ("ga4", "cloudflare"):
        return None
    if integration is None:
        if not _feature_enabled():
            return None
        integration = integrations_for(website).get(source)
        if integration is None or _external_status(source, integration) != "ok":
            return None

    if source == "ga4":
        ttl = 30 if period in ga4_dashboard.REALTIME_PERIODS else 300
        fetch = lambda: _fetch_ga4(website, integration, period)  # noqa: E731
    else:
        ttl = int(getattr(settings, "CLOUDFLARE_SNAPSHOT_TTL_SECONDS", 120))
        fetch = lambda: _fetch_cloudflare(website, integration, period)  # noqa: E731

    bundle = snapshots.get_or_fetch(
        f"dash:{source}:{period}", website.id, fetch, ttl=ttl
    )
    if not isinstance(bundle, dict) or bundle.get("pending"):
        return None
    return bundle


def dashboard_slice(website, slice_name: str, period: str):
    """External-source payload for one Overview endpoint, or None when
    the pixel path should serve (pixel has data, nothing connected,
    feature off, unknown slice, or the external build failed with no
    stale copy)."""
    if slice_name not in SLICES or not _feature_enabled():
        return None
    if _pixel_has_data(website.id):
        return None

    source, integration = _active_source(website)
    if source is None:
        return None

    bundle = fetch_bundle(website, source, period, integration=integration)
    if bundle is None:
        return None
    return bundle.get(slice_name)


# -- Source states (traffic pages) -------------------------------------------


def integrations_for(website) -> dict:
    """{source key: Integration or None} for ga4, ga_hosted and cloudflare
    (one query; inactive rows included, the states judge them)."""
    from apps.websites.models import Integration

    rows = {
        i.type: i
        for i in Integration.objects.filter(
            website=website, type__in=tuple(_INTEGRATION_TYPES.values())
        )
    }
    return {key: rows.get(type_) for key, type_ in _INTEGRATION_TYPES.items()}


def _external_status(source: str, integration) -> str:
    """ok / pending_selection / not_connected for GA4, the hosted tag or
    Cloudflare, by the same rules as _active_source."""
    if integration is None or not integration.is_active:
        return "not_connected"
    metadata = integration.metadata or {}
    if source == "ga4":
        if not (integration.access_token or integration.refresh_token):
            return "not_connected"
        return "ok" if metadata.get("property_id") else "pending_selection"
    if source == "cloudflare":
        if not integration.access_token:
            return "not_connected"
        return "ok" if metadata.get("zone_id") else "pending_selection"
    return "ok" if metadata.get("measurement_id") else "not_connected"


def pixel_span(website_id):
    """(first, last) pixel pageview timestamps, or (None, None) before the
    first one. Cached a minute once the pixel has recorded something."""
    key = f"wa:pixelspan:{website_id}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    from apps.analytics.models import PageEvent

    events = PageEvent.objects.filter(website_id=website_id)
    # Pageviews first: (website, event_type, timestamp) is indexed, so
    # this is two index lookups even on a busy site.
    span = events.filter(event_type="pageview").aggregate(
        first=Min("timestamp"), last=Max("timestamp")
    )
    if span["first"] is None:
        span = events.aggregate(first=Min("timestamp"), last=Max("timestamp"))
    value = (span["first"], span["last"])
    if value[0] is not None:
        try:
            cache.set(key, value, PIXEL_SPAN_TTL_SECONDS)
        except Exception:
            pass
    return value


def source_states(website, *, integrations: dict | None = None) -> dict[str, dict]:
    """Per source key: {status, connected, has_data, detail, measured_since}
    (contract 4b); Cloudflare adds ai_crawlers (True, False or "unknown").

    GA4 and Cloudflare are judged independently, so both can be ok.
    """
    if integrations is None:
        integrations = integrations_for(website)
    enabled = _feature_enabled()

    has_pixel = _pixel_has_data(website.id)
    first, last = pixel_span(website.id) if has_pixel else (None, None)
    states = {
        "pixel": {
            "status": "ok" if has_pixel else "not_installed",
            "connected": bool(has_pixel or website.pixel_verified),
            "has_data": has_pixel,
            "detail": f"Last event {last.strftime('%Y-%m-%dT%H:%M:%SZ')}" if last else "",
            "measured_since": first.date().isoformat() if first else None,
        },
    }

    detail_keys = {
        "ga4": "property_display_name",
        "ga_hosted": "measurement_id",
        "cloudflare": "zone_name",
    }
    for source, detail_key in detail_keys.items():
        integration = integrations.get(source)
        status = _external_status(source, integration) if enabled else "disabled"
        metadata = (integration.metadata if integration is not None else None) or {}
        states[source] = {
            "status": status,
            "connected": status in ("ok", "pending_selection"),
            # GA4 and Cloudflare are read on demand; the hosted tag is live
            # only and never serves a period.
            "has_data": status == "ok" and source != "ga_hosted",
            "detail": (metadata.get(detail_key) or "") if status == "ok" else "",
            "measured_since": None,
        }
        if source == "cloudflare":
            states[source]["ai_crawlers"] = (
                ai_crawlers.capability(metadata.get("zone_id")) if status == "ok" else "unknown"
            )
    return states


def auto_source(website, *, states: dict | None = None) -> str | None:
    """What source=auto reads: the pixel when it has any event, else GA4,
    else Cloudflare (each when usable), else an installed pixel without
    events, else None."""
    if states is None:
        states = source_states(website)
    if states["pixel"]["has_data"]:
        return "pixel"
    for source in ("ga4", "cloudflare"):
        if states[source]["status"] == "ok":
            return source
    if states["pixel"]["connected"]:
        return "pixel"
    return None
