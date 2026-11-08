"""REST endpoints for external traffic sources (GA4, hosted tag, Cloudflare).

Connection endpoints manage per-website Integration rows; snapshot
endpoints serve read-through Redis caches and never write analytics
rows to Postgres. Everything is mounted at /api/v1/web-analytics/ —
deliberately NOT under /api/v1/analytics/, whose middleware writes an
audit DB row per GET (this surface is polled every 30s).

All website-scoped views inherit TenantScopedAPIView via the feature
gate below; the OAuth callback is the one AllowAny view (trust = the
signed state parameter). The whole app is dark-launchable: with
WEB_ANALYTICS_ENABLED off every endpoint 404s.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.http import HttpResponseRedirect
from requests import RequestException
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.web_analytics.services import (
    cloudflare_client,
    ga4_client,
    ga4_hosted,
    ga4_oauth,
    snapshots,
)
from core.views import TenantScopedAPIView

logger = logging.getLogger("apps")


def _feature_enabled() -> bool:
    return bool(getattr(settings, "WEB_ANALYTICS_ENABLED", True))


class GatedTenantView(TenantScopedAPIView):
    """Tenant-scoped view that 404s while the feature flag is off."""

    def initial(self, request, *args, **kwargs):
        if not _feature_enabled():
            raise NotFound()
        super().initial(request, *args, **kwargs)


def _get_integration(website, type_: str):
    from apps.websites.models import Integration

    return Integration.objects.filter(website=website, type=type_).first()


# ── GA4 (client's own property, OAuth) ──────────────────────────────────────


def _ga4_status_payload(integration) -> dict:
    connected = bool(integration and (integration.access_token or integration.refresh_token))
    metadata = (integration.metadata if integration else None) or {}
    return {
        "connected": connected,
        "is_active": bool(integration and integration.is_active and connected),
        "property_id": metadata.get("property_id"),
        "property_display_name": metadata.get("property_display_name"),
        "pending_property_selection": bool(metadata.get("pending_property_selection")),
        "connected_at": integration.connected_at if connected else None,
        "configured": ga4_oauth.is_configured(),
        "feature_enabled": _feature_enabled(),
    }


class Ga4ConnectStartView(GatedTenantView):
    """Start the OAuth flow: return the Google consent URL."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        if not ga4_oauth.is_configured():
            return Response({"error": "not_configured"}, status=status.HTTP_400_BAD_REQUEST)
        url = ga4_oauth.build_authorize_url(website=website, user=request.user)
        return Response({"authorize_url": url})


class Ga4OAuthCallbackView(APIView):
    """Google redirects here after consent. Always 302s back to the SPA."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def _spa_redirect(self, website_id, outcome: str, reason: str = "") -> HttpResponseRedirect:
        # GA4 is managed in Settings > Connections; the SPA makes the
        # named website active before it reads the result.
        from urllib.parse import urlencode

        base = settings.FRONTEND_URL.rstrip("/")
        query = {"ga4": outcome}
        if website_id:
            query["website"] = str(website_id)
        if reason:
            query["reason"] = reason
        return HttpResponseRedirect(f"{base}/settings/connections?{urlencode(query)}")

    def get(self, request):
        if not _feature_enabled():
            raise NotFound()

        state_raw = request.query_params.get("state", "")
        try:
            state = ga4_oauth.parse_state(state_raw)
        except signing.BadSignature:
            logger.warning("GA4 callback received an invalid state parameter")
            return self._spa_redirect(None, "error", "invalid_state")

        website_id = state.get("website_id")

        if request.query_params.get("error"):
            return self._spa_redirect(website_id, "error", "denied")

        from apps.websites.models import Website

        try:
            website = Website.objects.get(id=website_id, user_id=state.get("user_id"))
        except (Website.DoesNotExist, ValueError):
            return self._spa_redirect(None, "error", "invalid_state")

        code = request.query_params.get("code", "")
        try:
            tokens = ga4_oauth.exchange_code(code)
        except RequestException as exc:
            logger.warning("GA4 code exchange failed: %s", exc)
            return self._spa_redirect(website_id, "error", "exchange_failed")

        integration = ga4_oauth.complete_connection(website=website, tokens=tokens)
        selected = ga4_oauth.auto_select_property(integration)
        return self._spa_redirect(website_id, "connected" if selected else "select_property")


class Ga4StatusView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(_ga4_status_payload(_get_integration(website, "ga")))


class Ga4PropertiesView(GatedTenantView):
    """List the GA4 properties the connected Google account can read."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "ga")
        if integration is None or not integration.access_token:
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)
        properties = (integration.metadata or {}).get("available_properties")
        if not properties:
            properties = ga4_client.list_account_summaries(
                integration.access_token, website_id=str(website.id)
            )
        return Response({"properties": properties})


class Ga4SelectPropertyView(GatedTenantView):
    """Pin the GA4 property whose realtime data feeds this website."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "ga")
        if integration is None or not integration.access_token:
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)

        property_id = str(request.data.get("property_id") or "").strip()
        if not property_id:
            return Response(
                {"error": "property_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )
        if not ga4_oauth.select_property(integration, property_id):
            return Response(
                {"error": "property_id is not an accessible property"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        snapshots.invalidate("ga4", website.id)
        return Response(_ga4_status_payload(integration))


class Ga4DisconnectView(GatedTenantView):
    def delete(self, request, website_id):
        website = self.get_website(website_id, min_role="admin")
        integration = _get_integration(website, "ga")
        if integration is not None:
            ga4_oauth.disconnect(integration)
        snapshots.invalidate("ga4", website.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class Ga4RealtimeSnapshotView(GatedTenantView):
    """Read-through realtime snapshot for the connected GA4 property."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "ga")
        metadata = (integration.metadata if integration else None) or {}
        property_id = metadata.get("property_id")
        if not (integration and integration.is_active and property_id):
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)

        def fetch():
            if integration.needs_token_refresh():
                try:
                    ga4_oauth.refresh_access_token(integration)
                except Exception as exc:
                    logger.warning("GA4 inline token refresh failed: %s", exc)
            if not integration.is_active or not integration.access_token:
                return None
            return ga4_client.build_realtime_snapshot(
                integration.access_token, property_id, website_id=str(website.id)
            )

        payload = snapshots.get_or_fetch(
            "ga4",
            website.id,
            fetch,
            ttl=int(getattr(settings, "GA4_SNAPSHOT_TTL_SECONDS", 25)),
        )
        return Response(payload)


# ── Hosted Google tag (Cansee-owned pool property) ────────────────────────


def _hosted_status_payload(integration) -> dict:
    metadata = (integration.metadata if integration else None) or {}
    measurement_id = metadata.get("measurement_id")
    enabled = bool(integration and integration.is_active and measurement_id)
    return {
        "enabled": enabled,
        "measurement_id": measurement_id if enabled else None,
        "snippet": ga4_hosted.snippet(measurement_id) if enabled else None,
        "provisioning_error": metadata.get("provisioning_error"),
        "configured": ga4_hosted.is_configured(),
        "feature_enabled": _feature_enabled(),
    }


class HostedStatusView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(_hosted_status_payload(_get_integration(website, "ga_hosted")))


class HostedEnableView(GatedTenantView):
    """Provision (or reactivate) this website's stream in the pool property."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        if not ga4_hosted.is_configured():
            return Response({"error": "not_configured"}, status=status.HTTP_400_BAD_REQUEST)
        integration, error = ga4_hosted.provision_stream(website)
        payload = _hosted_status_payload(integration)
        if error:
            return Response(payload, status=status.HTTP_502_BAD_GATEWAY)
        return Response(payload)


class HostedDisableView(GatedTenantView):
    def delete(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "ga_hosted")
        if integration is not None:
            ga4_hosted.disable(integration)
        snapshots.invalidate("ga4h", website.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class HostedRealtimeSnapshotView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "ga_hosted")
        metadata = (integration.metadata if integration else None) or {}
        if not (integration and integration.is_active and metadata.get("stream_id")):
            return Response({"error": "not_enabled"}, status=status.HTTP_409_CONFLICT)

        payload = snapshots.get_or_fetch(
            "ga4h",
            website.id,
            lambda: ga4_hosted.build_hosted_snapshot(website, integration),
            ttl=int(getattr(settings, "GA4_SNAPSHOT_TTL_SECONDS", 25)),
        )
        return Response(payload)


# ── Cloudflare zone (tenant API token) ──────────────────────────────────────


def _website_host(website) -> str:
    host = urlparse(website.url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _match_zone(website, zones: list[dict]) -> dict | None:
    """Pick the zone whose name is the site host or a parent of it."""
    host = _website_host(website)
    if not host:
        return None
    matches = [
        z for z in zones
        if host == z["name"] or host.endswith("." + z["name"])
    ]
    if not matches:
        return None
    return max(matches, key=lambda z: len(z["name"]))


def _cloudflare_status_payload(integration) -> dict:
    connected = bool(integration and integration.access_token)
    metadata = (integration.metadata if integration else None) or {}
    return {
        "connected": connected,
        "is_active": bool(integration and integration.is_active and connected),
        "zone_id": metadata.get("zone_id"),
        "zone_name": metadata.get("zone_name"),
        "pending_zone_selection": bool(metadata.get("pending_zone_selection")),
        "zones_found": len(metadata.get("available_zones") or []),
        "connected_at": integration.connected_at if connected else None,
        "feature_enabled": _feature_enabled(),
    }


class CloudflareConnectView(GatedTenantView):
    """Store a tenant-supplied zone-analytics token and auto-match the zone."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        api_token = str(request.data.get("api_token") or "").strip()
        if not api_token:
            return Response(
                {"error": "api_token is required"}, status=status.HTTP_400_BAD_REQUEST
            )
        if not cloudflare_client.verify_token(api_token):
            return Response({"error": "invalid_token"}, status=status.HTTP_400_BAD_REQUEST)

        from apps.websites.models import Integration

        zones = cloudflare_client.list_zones(api_token)
        integration, _created = Integration.objects.get_or_create(
            website=website, type="cloudflare"
        )
        integration.access_token = api_token
        integration.is_active = True
        metadata = dict(integration.metadata or {})
        metadata["available_zones"] = zones
        match = _match_zone(website, zones)
        if match:
            metadata["zone_id"] = match["id"]
            metadata["zone_name"] = match["name"]
            metadata.pop("pending_zone_selection", None)
        else:
            metadata.pop("zone_id", None)
            metadata.pop("zone_name", None)
            metadata["pending_zone_selection"] = True
        integration.metadata = metadata
        integration.save()
        snapshots.invalidate("cf", website.id)
        return Response(_cloudflare_status_payload(integration))


class CloudflareStatusView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(_cloudflare_status_payload(_get_integration(website, "cloudflare")))


class CloudflareZonesView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "cloudflare")
        if integration is None or not integration.access_token:
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)
        zones = (integration.metadata or {}).get("available_zones")
        if not zones:
            zones = cloudflare_client.list_zones(integration.access_token)
        return Response({"zones": zones})


class CloudflareSelectZoneView(GatedTenantView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "cloudflare")
        if integration is None or not integration.access_token:
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)

        zone_id = str(request.data.get("zone_id") or "").strip()
        if not zone_id:
            return Response({"error": "zone_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        zones = (integration.metadata or {}).get("available_zones") or []
        by_id = {z["id"]: z for z in zones}
        if not by_id:
            by_id = {z["id"]: z for z in cloudflare_client.list_zones(integration.access_token)}
        chosen = by_id.get(zone_id)
        if chosen is None:
            return Response(
                {"error": "zone_id is not an accessible zone"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        metadata = dict(integration.metadata or {})
        metadata["zone_id"] = chosen["id"]
        metadata["zone_name"] = chosen["name"]
        metadata["available_zones"] = list(by_id.values())
        metadata.pop("pending_zone_selection", None)
        integration.metadata = metadata
        integration.save(update_fields=["metadata", "updated_at"])
        snapshots.invalidate("cf", website.id)
        return Response(_cloudflare_status_payload(integration))


class CloudflareDisconnectView(GatedTenantView):
    def delete(self, request, website_id):
        website = self.get_website(website_id, min_role="admin")
        integration = _get_integration(website, "cloudflare")
        if integration is not None:
            # Nothing to revoke remotely; the tenant owns the token.
            integration.access_token = ""
            integration.is_active = False
            metadata = dict(integration.metadata or {})
            for key in ("zone_id", "zone_name", "available_zones", "pending_zone_selection"):
                metadata.pop(key, None)
            integration.metadata = metadata
            integration.save()
        snapshots.invalidate("cf", website.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CloudflareSnapshotView(GatedTenantView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website, "cloudflare")
        metadata = (integration.metadata if integration else None) or {}
        zone_id = metadata.get("zone_id")
        if not (integration and integration.is_active and integration.access_token and zone_id):
            return Response({"error": "not_connected"}, status=status.HTTP_409_CONFLICT)

        zone_name = metadata.get("zone_name")

        def fetch():
            payload = cloudflare_client.build_zone_snapshot(integration.access_token, zone_id)
            if payload is not None and zone_name:
                payload["zone_name"] = zone_name
            return payload

        payload = snapshots.get_or_fetch(
            "cf",
            website.id,
            fetch,
            ttl=int(getattr(settings, "CLOUDFLARE_SNAPSHOT_TTL_SECONDS", 120)),
        )
        return Response(payload)
