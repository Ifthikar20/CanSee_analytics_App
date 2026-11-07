"""Traffic and AI traffic API (pages contract section 4).

GET <website_id>/traffic/sources/     what each source measures, and the default
GET <website_id>/traffic/             visitors, pages, referrers, devices, countries
GET <website_id>/traffic/countries/   the country part only (one upstream report)
GET <website_id>/ai-traffic/          AI-referred visits and AI crawler requests

TenantScopedAPIView (member and above), not GatedTenantView: the pixel
works with WEB_ANALYTICS_ENABLED off. A source that is missing, empty or
failing is a 200 payload with a reason, never a 404. Read-only; upstream
calls are cached in services.snapshots.
"""

from rest_framework.response import Response

from apps.web_analytics.services import ai_traffic, traffic
from core.views import TenantScopedAPIView


class TrafficSourcesView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(traffic.traffic_sources(website))


class TrafficView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        params = request.query_params
        period = traffic.parse_period(params.get("period"))
        source = traffic.parse_source(params.get("source"))
        return Response(traffic.traffic(website, period=period, source=source))


class TrafficCountriesView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        params = request.query_params
        period = traffic.parse_period(params.get("period"))
        source = traffic.parse_source(params.get("source"))
        return Response(traffic.traffic_countries(website, period=period, source=source))


class AiTrafficView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        params = request.query_params
        period = traffic.parse_period(params.get("period"))
        source = traffic.parse_source(params.get("source"), ai_traffic.SOURCES)
        return Response(ai_traffic.ai_traffic(website, period=period, source=source))
