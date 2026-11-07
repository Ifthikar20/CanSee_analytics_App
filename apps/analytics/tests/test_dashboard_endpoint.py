"""The merged dashboard endpoint.

Rendering the dashboard used to be five requests. Each resolved the tenant
again and paid the full middleware stack - rate-limit counters and throttle
checks in Redis, an audit-log line, JWT verification - so the repetition
cost more than the SQL did.

The merge is additive on purpose: the five original endpoints are
untouched, so the frontend can migrate one panel at a time. The last test
in this file is what guarantees that.
"""
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.analytics.tests.test_analytics import (
    create_test_user,
    create_test_website,
    seed_analytics_data,
)

SECTIONS = {"overview", "top_pages", "traffic_sources", "realtime", "ai_traffic"}

# Every <400 response is wrapped by core.interceptors.response_envelope,
# so the payload lives under "data".
def body(response):
    return response.json()["data"]


class DashboardEndpointTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=7, visitors_per_day=2)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.wid = str(self.website.id)
        self.url = f"/api/v1/analytics/{self.wid}/dashboard/"

    def test_returns_every_panel_in_one_response(self):
        resp = self.client.get(self.url)
        assert resp.status_code == 200
        data = body(resp)
        assert SECTIONS.issubset(data.keys())
        assert data["period"] == "30d"
        assert "errors" not in data

    def test_period_is_passed_through(self):
        data = body(self.client.get(self.url + "?period=7d"))
        assert data["period"] == "7d"
        assert data["overview"]["period"] == "7d"

    def test_sections_parameter_limits_the_work(self):
        data = body(self.client.get(self.url + "?sections=overview,top_pages"))
        assert "overview" in data
        assert "top_pages" in data
        assert "realtime" not in data
        assert "ai_traffic" not in data

    def test_unknown_section_names_are_ignored_not_errors(self):
        data = body(self.client.get(self.url + "?sections=overview,nonsense"))
        assert "overview" in data
        assert "nonsense" not in data

    def test_one_broken_panel_does_not_blank_the_dashboard(self):
        """The single thing five separate requests did better.

        With one endpoint, an exception in any service would take the whole
        page down unless sections are isolated. This pins that they are.
        """
        with patch(
            "apps.analytics.services.analytics_service.AnalyticsService.get_realtime_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.get(self.url)

        assert resp.status_code == 200
        data = body(resp)
        assert data["realtime"] is None
        assert data["errors"] == {"realtime": "unavailable"}
        # The panels that did work are still present and populated.
        assert data["overview"]["total_visitors"] >= 0
        assert isinstance(data["top_pages"], list)

    def test_costs_fewer_queries_than_the_five_separate_endpoints(self):
        """The whole point of the merge, measured rather than asserted."""
        separate = [
            f"/api/v1/analytics/{self.wid}/overview/",
            f"/api/v1/analytics/{self.wid}/pages/",
            f"/api/v1/analytics/{self.wid}/sources/",
            f"/api/v1/analytics/{self.wid}/realtime/",
            f"/api/v1/analytics/{self.wid}/ai-traffic/",
        ]
        with CaptureQueriesContext(connection) as five:
            for url in separate:
                assert self.client.get(url).status_code == 200
        with CaptureQueriesContext(connection) as one:
            assert self.client.get(self.url).status_code == 200

        # Printed so the saving stays visible in CI output rather than
        # being a number somebody has to go and re-derive.
        print(f"  MEASURED: five endpoints={len(five)} queries, merged={len(one)}")

        assert len(one) < len(five), (
            f"merged endpoint used {len(one)} queries, "
            f"five separate used {len(five)}"
        )

    def test_the_five_original_endpoints_still_work(self):
        """The additive guarantee. If this fails, the merge became a
        breaking change and ftb-ui cannot migrate incrementally."""
        for path in ("overview", "pages", "sources", "realtime", "ai-traffic"):
            resp = self.client.get(f"/api/v1/analytics/{self.wid}/{path}/")
            assert resp.status_code == 200, f"{path} broke"
