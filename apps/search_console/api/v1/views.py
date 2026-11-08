"""REST endpoints for the Google Search Console integration.

Connection endpoints manage the per-website Integration(type="gsc")
row via the offline OAuth flow; data endpoints serve the ingested
Search Analytics rows. All website-scoped views inherit
TenantScopedAPIView so the caller must own the website. The OAuth
callback is the one AllowAny view: the browser arrives from Google
without a JWT, and trust comes from the signed state parameter.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.core import signing
from django.db.models import F, FloatField, Sum
from django.db.models.functions import Cast
from django.http import HttpResponseRedirect
from django.utils import timezone
from requests import RequestException
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.search_console.models import GscDailyTotal, GscPageStat, GscQueryStat
from apps.search_console.services import gsc_client, oauth_service
from core.views import TenantScopedAPIView

logger = logging.getLogger("apps")

DATA_LAG_DAYS = 3
DEFAULT_RANGE_DAYS = 28
MAX_RANGE_DAYS = 366
ORDER_FIELDS = {"clicks", "impressions", "ctr", "position"}


def _get_integration(website):
    from apps.websites.models import Integration

    return Integration.objects.filter(website=website, type="gsc").first()


def _status_payload(website, integration) -> dict:
    connected = bool(integration and (integration.access_token or integration.refresh_token))
    metadata = (integration.metadata if integration else None) or {}
    return {
        "connected": connected,
        "is_active": bool(integration and integration.is_active and connected),
        "site_url": metadata.get("site_url"),
        "pending_property_selection": bool(metadata.get("pending_property_selection")),
        "connected_at": integration.connected_at if connected else None,
        "last_synced_at": integration.last_synced_at if integration else None,
        "has_data": GscDailyTotal.objects.filter(website=website).exists(),
        "configured": oauth_service.is_configured(),
    }


def _parse_range(request) -> tuple[date, date]:
    """Read start/end query params with lag-aware defaults. Raises ValueError.

    Every message raised here reaches the client verbatim, so raise only
    fixed strings — never Python's own parse errors, which reflect the
    raw query input back in the response.
    """
    end_default = timezone.now().date() - timedelta(days=DATA_LAG_DAYS)
    start_raw = request.query_params.get("start")
    end_raw = request.query_params.get("end")
    try:
        end = date.fromisoformat(end_raw) if end_raw else end_default
        start = date.fromisoformat(start_raw) if start_raw else end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    except ValueError:
        raise ValueError("start and end must be YYYY-MM-DD dates") from None
    if start > end:
        raise ValueError("start must be on or before end")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"date range is capped at {MAX_RANGE_DAYS} days")
    return start, end


def _totals_for(website, start: date, end: date) -> dict:
    # Aggregate aliases must not shadow the model fields referenced in
    # the weighted expression, or Django resolves F("impressions") to
    # the aggregate and rejects the query.
    agg = GscDailyTotal.objects.filter(
        website=website, date__gte=start, date__lte=end
    ).aggregate(
        total_clicks=Sum("clicks"),
        total_impressions=Sum("impressions"),
        weighted_position=Sum(
            Cast(F("position"), FloatField()) * Cast(F("impressions"), FloatField())
        ),
    )
    clicks = agg["total_clicks"] or 0
    impressions = agg["total_impressions"] or 0
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": (clicks / impressions) if impressions else 0.0,
        "position": (agg["weighted_position"] / impressions) if impressions else 0.0,
    }


def _delta(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return (current - previous) / previous


# -- Connection ---------------------------------------------------------------


class GscConnectStartView(TenantScopedAPIView):
    """Start the OAuth flow: return the Google consent URL."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        if not oauth_service.is_configured():
            return Response(
                {"error": "not_configured"}, status=status.HTTP_400_BAD_REQUEST
            )
        url = oauth_service.build_authorize_url(website=website, user=request.user)
        return Response({"authorize_url": url})


class GscOAuthCallbackView(APIView):
    """Google redirects here after consent. Always 302s back to the SPA."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def _spa_redirect(self, website_id, outcome: str, reason: str = "") -> HttpResponseRedirect:
        # GSC is connected from Settings > Connections. The UI still
        # redirects the old /app/integrations path, for links in flight.
        base = settings.FRONTEND_URL.rstrip("/")
        url = f"{base}/settings/connections?gsc={outcome}"
        if website_id:
            url += f"&website_id={website_id}"
        if reason:
            url += f"&reason={reason}"
        return HttpResponseRedirect(url)

    def get(self, request):
        state_raw = request.query_params.get("state", "")
        try:
            state = oauth_service.parse_state(state_raw)
        except signing.BadSignature:
            logger.warning("GSC callback received an invalid state parameter")
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
            tokens = oauth_service.exchange_code(code)
        except RequestException as exc:
            logger.warning("GSC code exchange failed: %s", exc)
            return self._spa_redirect(website_id, "error", "exchange_failed")

        integration = oauth_service.complete_connection(website=website, tokens=tokens)
        match = oauth_service.auto_match_property(integration)

        if match:
            from apps.search_console.tasks import sync_website_gsc

            sync_website_gsc.delay(
                integration_id=integration.pk,
                backfill_days=int(getattr(settings, "GSC_BACKFILL_DAYS", 28)),
            )
            return self._spa_redirect(website_id, "connected")
        return self._spa_redirect(website_id, "select_property")


class GscStatusView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(_status_payload(website, _get_integration(website)))


class GscPropertiesView(TenantScopedAPIView):
    """List the Search Console properties the connected account can read."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website)
        if integration is None or not integration.access_token:
            return Response(
                {"error": "not_connected"}, status=status.HTTP_409_CONFLICT
            )
        sites = (integration.metadata or {}).get("available_sites")
        if not sites:
            sites = gsc_client.list_sites(integration.access_token)
        return Response({"properties": sites})


class GscSelectPropertyView(TenantScopedAPIView):
    """Pin the GSC property for this website and start the backfill."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        integration = _get_integration(website)
        if integration is None or not integration.access_token:
            return Response(
                {"error": "not_connected"}, status=status.HTTP_409_CONFLICT
            )

        site_url = (request.data.get("site_url") or "").strip()
        if not site_url:
            return Response(
                {"error": "site_url is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        available = {
            s["site_url"]
            for s in (integration.metadata or {}).get("available_sites", [])
        }
        if not available:
            available = {s["site_url"] for s in gsc_client.list_sites(integration.access_token)}
        if site_url not in available:
            return Response(
                {"error": "site_url is not an accessible property"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        metadata = dict(integration.metadata or {})
        metadata["site_url"] = site_url
        metadata.pop("pending_property_selection", None)
        integration.metadata = metadata
        integration.save(update_fields=["metadata", "updated_at"])

        from apps.search_console.tasks import sync_website_gsc

        sync_website_gsc.delay(
            integration_id=integration.pk,
            backfill_days=int(getattr(settings, "GSC_BACKFILL_DAYS", 28)),
        )
        return Response(_status_payload(website, integration))


class GscDisconnectView(TenantScopedAPIView):
    def delete(self, request, website_id):
        website = self.get_website(website_id, min_role="admin")
        integration = _get_integration(website)
        if integration is not None:
            oauth_service.disconnect(integration)
        return Response(status=status.HTTP_204_NO_CONTENT)


# -- Data ---------------------------------------------------------------------


class GscSummaryView(TenantScopedAPIView):
    """Range totals with previous-period comparison.

    Reads GscDailyTotal only: per-query rows undercount because Google
    anonymizes long-tail queries, so summing GscQueryStat would not
    reconcile with what Search Console shows the user.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        try:
            start, end = _parse_range(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        current = _totals_for(website, start, end)
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        previous = _totals_for(website, prev_start, prev_end)

        return Response({
            "start": start.isoformat(),
            "end": end.isoformat(),
            **current,
            "previous": {
                "start": prev_start.isoformat(),
                "end": prev_end.isoformat(),
                **previous,
            },
            "deltas": {
                key: _delta(current[key], previous[key])
                for key in ("clicks", "impressions", "ctr", "position")
            },
        })


class GscTimeseriesView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        try:
            start, end = _parse_range(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        rows = (
            GscDailyTotal.objects.filter(website=website, date__gte=start, date__lte=end)
            .order_by("date")
            .values("date", "clicks", "impressions", "ctr", "position")
        )
        series = [
            {
                "date": row["date"].isoformat(),
                "clicks": row["clicks"],
                "impressions": row["impressions"],
                "ctr": row["ctr"],
                "position": row["position"],
            }
            for row in rows
        ]
        return Response({"start": start.isoformat(), "end": end.isoformat(), "series": series})


class _GscDimensionListView(TenantScopedAPIView):
    """Shared aggregation for the queries/pages tables."""

    model = None
    dimension = ""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        try:
            start, end = _parse_range(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        order_raw = request.query_params.get("order", "-clicks")
        descending = order_raw.startswith("-")
        order_field = order_raw.lstrip("-")
        if order_field not in ORDER_FIELDS:
            return Response(
                {"error": f"order must be one of {sorted(ORDER_FIELDS)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            limit = min(max(int(request.query_params.get("limit", 50)), 1), 200)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except ValueError:
            return Response(
                {"error": "limit and offset must be integers"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = self.model.objects.filter(website=website, date__gte=start, date__lte=end)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(**{f"{self.dimension}__icontains": search})

        aggregated = (
            qs.values(self.dimension)
            .annotate(
                total_clicks=Sum("clicks"),
                total_impressions=Sum("impressions"),
                weighted_position=Sum(
                    Cast(F("position"), FloatField()) * Cast(F("impressions"), FloatField())
                ),
            )
            .order_by()
        )

        rows = []
        for row in aggregated:
            impressions = row["total_impressions"] or 0
            clicks = row["total_clicks"] or 0
            rows.append({
                self.dimension: row[self.dimension],
                "clicks": clicks,
                "impressions": impressions,
                "ctr": (clicks / impressions) if impressions else 0.0,
                "position": (row["weighted_position"] / impressions) if impressions else 0.0,
            })
        rows.sort(key=lambda r: r[order_field], reverse=descending)

        count = len(rows)
        page = rows[offset:offset + limit]

        # Previous-period comparison for just the rows on this page.
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        keys = [r[self.dimension] for r in page]
        prev = {
            row[self.dimension]: row
            for row in (
                self.model.objects.filter(
                    website=website,
                    date__gte=prev_start,
                    date__lte=prev_end,
                    **{f"{self.dimension}__in": keys},
                )
                .values(self.dimension)
                .annotate(
                    total_clicks=Sum("clicks"),
                    total_impressions=Sum("impressions"),
                    weighted_position=Sum(
                        Cast(F("position"), FloatField())
                        * Cast(F("impressions"), FloatField())
                    ),
                )
                .order_by()
            )
        }
        for row in page:
            p = prev.get(row[self.dimension])
            prev_clicks = (p["total_clicks"] or 0) if p else 0
            prev_impressions = (p["total_impressions"] or 0) if p else 0
            prev_position = (
                (p["weighted_position"] / prev_impressions)
                if p and prev_impressions
                else None
            )
            row["prev_clicks"] = prev_clicks
            row["clicks_delta"] = _delta(row["clicks"], prev_clicks)
            row["position_delta"] = (
                row["position"] - prev_position if prev_position is not None else None
            )

        # Key is "rows", not "results": the EnvelopeRenderer treats any
        # dict containing "results" as DRF pagination output and strips
        # the other keys while hoisting the list.
        return Response({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": count,
            "limit": limit,
            "offset": offset,
            "rows": page,
        })


class GscQueriesView(_GscDimensionListView):
    model = GscQueryStat
    dimension = "query"


class GscPagesView(_GscDimensionListView):
    model = GscPageStat
    dimension = "page"
