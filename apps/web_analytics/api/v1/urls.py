from django.urls import path

from apps.web_analytics.api.v1 import traffic_views, views

urlpatterns = [
    # OAuth callback (unauthenticated; trust = signed state)
    path("ga4/oauth/callback/", views.Ga4OAuthCallbackView.as_view(), name="wa-ga4-oauth-callback"),

    # Hosted Google tag (Cansee-owned pool property). Registered before
    # the <uuid:website_id> GA4 routes; "hosted" never parses as a UUID
    # anyway, but keeping the literal prefix first makes intent obvious.
    path("ga4/hosted/<uuid:website_id>/status/", views.HostedStatusView.as_view(), name="wa-hosted-status"),
    path("ga4/hosted/<uuid:website_id>/enable/", views.HostedEnableView.as_view(), name="wa-hosted-enable"),
    path("ga4/hosted/<uuid:website_id>/connection/", views.HostedDisableView.as_view(), name="wa-hosted-disable"),
    path("ga4/hosted/<uuid:website_id>/realtime/", views.HostedRealtimeSnapshotView.as_view(), name="wa-hosted-realtime"),

    # GA4 connect (client's own property via OAuth)
    path("ga4/<uuid:website_id>/connect/", views.Ga4ConnectStartView.as_view(), name="wa-ga4-connect"),
    path("ga4/<uuid:website_id>/status/", views.Ga4StatusView.as_view(), name="wa-ga4-status"),
    path("ga4/<uuid:website_id>/properties/", views.Ga4PropertiesView.as_view(), name="wa-ga4-properties"),
    path("ga4/<uuid:website_id>/property/", views.Ga4SelectPropertyView.as_view(), name="wa-ga4-select-property"),
    path("ga4/<uuid:website_id>/connection/", views.Ga4DisconnectView.as_view(), name="wa-ga4-disconnect"),
    path("ga4/<uuid:website_id>/realtime/", views.Ga4RealtimeSnapshotView.as_view(), name="wa-ga4-realtime"),

    # Cloudflare zone analytics (tenant API token)
    path("cloudflare/<uuid:website_id>/connect/", views.CloudflareConnectView.as_view(), name="wa-cf-connect"),
    path("cloudflare/<uuid:website_id>/status/", views.CloudflareStatusView.as_view(), name="wa-cf-status"),
    path("cloudflare/<uuid:website_id>/zones/", views.CloudflareZonesView.as_view(), name="wa-cf-zones"),
    path("cloudflare/<uuid:website_id>/zone/", views.CloudflareSelectZoneView.as_view(), name="wa-cf-select-zone"),
    path("cloudflare/<uuid:website_id>/connection/", views.CloudflareDisconnectView.as_view(), name="wa-cf-disconnect"),
    path("cloudflare/<uuid:website_id>/snapshot/", views.CloudflareSnapshotView.as_view(), name="wa-cf-snapshot"),

    # Traffic pages: whichever source the website has (pixel, GA4, Cloudflare)
    path("<uuid:website_id>/traffic/sources/", traffic_views.TrafficSourcesView.as_view(), name="wa-traffic-sources"),
    path("<uuid:website_id>/traffic/", traffic_views.TrafficView.as_view(), name="wa-traffic"),
    path("<uuid:website_id>/traffic/countries/", traffic_views.TrafficCountriesView.as_view(), name="wa-traffic-countries"),
    path("<uuid:website_id>/ai-traffic/", traffic_views.AiTrafficView.as_view(), name="wa-ai-traffic"),
]
