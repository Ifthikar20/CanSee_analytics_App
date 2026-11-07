"""
Comprehensive test suite for Cansee Analytics.
Tests: event ingestion, daily stats, funnels, retention, flows, AI insights,
keyword intelligence, and API endpoints.
"""
import uuid
from datetime import timedelta

from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.analytics.models import (
    CustomFunnel,
    PageEvent,
    Session,
    Visitor,
)
from apps.websites.models import Website

# A realistic non-bot user-agent. Ingestion now drops blank/bot UAs, so
# service-level tests that bypass the HTTP layer must supply one.
_HUMAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def create_test_user():
    """Create a test user for API tests."""
    from apps.accounts.models import User
    return User.objects.create_user(
        email="test@cansee.ai",
        password="TestPass123!",
        full_name="Test User",
    )


def create_test_website(user, name="Outfi", url="https://www.outfi.ai"):
    """Create a test website."""
    website = Website.objects.create(
        user=user,
        name=name,
        url=url,
        industry="Fashion",
        pixel_key=uuid.uuid4(),
        is_active=True,
    )
    return website


def seed_analytics_data(website, days=30, visitors_per_day=3):
    """Seed realistic analytics data for tests."""
    now = timezone.now()
    visitors = []
    sessions = []

    for d in range(days):
        ts = now - timedelta(days=d)
        for v in range(visitors_per_day):
            visitor, _ = Visitor.objects.get_or_create(
                website=website,
                fingerprint_hash=f"hash_{d}_{v}",
                defaults={
                    "geo_country": "US",
                    "geo_city": "New York",
                    "device_type": ["desktop", "mobile", "tablet"][v % 3],
                    "browser": "Chrome",
                    "os": "MacOS",
                },
            )
            visitors.append(visitor)

            session = Session.objects.create(
                visitor=visitor,
                started_at=ts,
                ended_at=ts + timedelta(minutes=5),
                page_count=3,
                entry_page=f"https://www.outfi.ai/page-{v}",
                exit_page=f"https://www.outfi.ai/exit-{v}",
                source=["google", "direct", "facebook"][v % 3],
                medium=["organic", "", "social"][v % 3],
            )
            sessions.append(session)

            for page_num in range(3):
                PageEvent.objects.create(
                    visitor=visitor,
                    website=website,
                    session=session,
                    url=f"https://www.outfi.ai/page-{page_num}",
                    event_type="pageview",
                    timestamp=ts + timedelta(minutes=page_num),
                )

            # Add a click event
            PageEvent.objects.create(
                visitor=visitor,
                website=website,
                session=session,
                url="https://www.outfi.ai/page-0",
                event_type="click",
                timestamp=ts + timedelta(minutes=1, seconds=30),
                properties={"x": 100, "y": 200},
            )

    return visitors, sessions


# ═══════════════════════════════════════════
# Event Ingestion Tests
# ═══════════════════════════════════════════

class EventIngestionServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)

    def test_ingest_pageview_creates_visitor_and_session(self):
        from apps.analytics.services.event_ingestion_service import EventIngestionService

        event = EventIngestionService.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data={
                "fingerprint": "unique-browser-fp-001",
                "url": "https://www.outfi.ai/shop",
                "event_type": "pageview",
                "referrer": "https://www.google.com/search?q=outfi",
                "user_agent": _HUMAN_UA,
            },
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "pageview")
        self.assertEqual(event.url, "https://www.outfi.ai/shop")

        # Visitor should be created
        self.assertEqual(Visitor.objects.filter(website=self.website).count(), 1)

        # Session should be auto-created
        self.assertEqual(Session.objects.filter(visitor=event.visitor).count(), 1)
        session = Session.objects.first()
        self.assertEqual(session.source, "google")
        self.assertEqual(session.medium, "organic")

    def test_ingest_with_utm_params(self):
        from apps.analytics.services.event_ingestion_service import EventIngestionService

        EventIngestionService.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data={
                "fingerprint": "utm-test-fp",
                "url": "https://www.outfi.ai/?utm_source=newsletter&utm_medium=email&utm_campaign=spring_sale",
                "event_type": "pageview",
                "user_agent": _HUMAN_UA,
            },
        )

        session = Session.objects.first()
        self.assertEqual(session.source, "newsletter")
        self.assertEqual(session.medium, "email")
        self.assertEqual(session.campaign, "spring_sale")

    def test_ingest_invalid_pixel_key_raises(self):
        from apps.analytics.services.event_ingestion_service import EventIngestionService

        with self.assertRaises((ValueError, Exception)):  # noqa: B017
            EventIngestionService.ingest_event(
                pixel_key="invalid-key",
                event_data={"fingerprint": "x", "url": "/", "event_type": "pageview"},
            )

    def test_ingest_batch(self):
        from apps.analytics.services.event_ingestion_service import EventIngestionService

        events = [
            {"fingerprint": f"batch-{i}", "url": f"https://www.outfi.ai/p{i}", "event_type": "pageview", "user_agent": _HUMAN_UA}
            for i in range(5)
        ]
        results = EventIngestionService.ingest_batch(
            pixel_key=str(self.website.pixel_key), events=events
        )
        self.assertEqual(len(results), 5)
        self.assertEqual(PageEvent.objects.filter(website=self.website).count(), 5)

    def test_session_reuse_within_30_min(self):
        from apps.analytics.services.event_ingestion_service import EventIngestionService

        # First event
        e1 = EventIngestionService.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data={"fingerprint": "reuse-fp", "url": "https://www.outfi.ai/a", "event_type": "pageview", "user_agent": _HUMAN_UA},
        )
        # Second event within 30 min
        e2 = EventIngestionService.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data={"fingerprint": "reuse-fp", "url": "https://www.outfi.ai/b", "event_type": "pageview", "user_agent": _HUMAN_UA},
        )
        # Should reuse same session
        self.assertEqual(e1.session_id, e2.session_id)


# ═══════════════════════════════════════════
# Daily Stats Tests
# ═══════════════════════════════════════════

class DailyStatsServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=10, visitors_per_day=2)

    def test_get_chart_data(self):
        from apps.analytics.services.daily_stats import DailyStatsService
        data = DailyStatsService.get_chart_data(website_id=str(self.website.id), period="30d")
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) > 0)
        self.assertIn("date", data[0])
        self.assertIn("visitors", data[0])
        self.assertIn("pageviews", data[0])

    def test_get_device_breakdown(self):
        from apps.analytics.services.daily_stats import DailyStatsService
        data = DailyStatsService.get_device_breakdown(website_id=str(self.website.id))
        self.assertIsInstance(data, list)
        device_names = [d["name"] for d in data]
        self.assertTrue(any(name in device_names for name in ["Desktop", "Mobile", "Tablet"]))

    def test_get_country_breakdown(self):
        from apps.analytics.services.daily_stats import DailyStatsService
        data = DailyStatsService.get_country_breakdown(website_id=str(self.website.id))
        self.assertIsInstance(data, list)
        if data:
            self.assertIn("name", data[0])
            self.assertIn("visitors", data[0])

    def test_get_bounce_rate(self):
        from apps.analytics.services.daily_stats import DailyStatsService
        rate = DailyStatsService.get_bounce_rate(website_id=str(self.website.id))
        self.assertIsInstance(rate, float)
        self.assertTrue(0 <= rate <= 100)

    def test_get_avg_session_duration(self):
        from apps.analytics.services.daily_stats import DailyStatsService
        duration = DailyStatsService.get_avg_session_duration(website_id=str(self.website.id))
        self.assertIsInstance(duration, str)
        self.assertIn(":", duration)


# ═══════════════════════════════════════════
# Funnel Tests
# ═══════════════════════════════════════════

class FunnelServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=7, visitors_per_day=3)

    def test_create_funnel(self):
        from apps.analytics.services.funnel_service import FunnelService
        funnel = FunnelService.create_funnel(
            website_id=str(self.website.id),
            name="Outfi Purchase Flow",
            steps=[
                {"name": "Landing", "type": "url", "value": "page-0"},
                {"name": "Product", "type": "url", "value": "page-1"},
                {"name": "Checkout", "type": "url", "value": "page-2"},
            ],
            user=self.user,
        )
        self.assertEqual(funnel["name"], "Outfi Purchase Flow")
        self.assertEqual(CustomFunnel.objects.count(), 1)

    def test_calculate_funnel(self):
        from apps.analytics.services.funnel_service import FunnelService
        # Create funnel
        f = FunnelService.create_funnel(
            website_id=str(self.website.id),
            name="Test Funnel",
            steps=[
                {"name": "Page 0", "type": "url", "value": "page-0"},
                {"name": "Page 1", "type": "url", "value": "page-1"},
            ],
        )
        # Calculate
        result = FunnelService.calculate_funnel(
            website_id=str(self.website.id), funnel_id=f["id"]
        )
        self.assertIn("steps", result)
        self.assertEqual(len(result["steps"]), 2)
        self.assertIn("overall_conversion_pct", result)
        # First step should have visitors
        self.assertGreater(result["steps"][0]["visitors"], 0)

    def test_list_funnels(self):
        from apps.analytics.services.funnel_service import FunnelService
        FunnelService.create_funnel(
            website_id=str(self.website.id), name="F1", steps=[{"name": "A", "type": "url", "value": "/a"}, {"name": "B", "type": "url", "value": "/b"}]
        )
        funnels = FunnelService.list_funnels(website_id=str(self.website.id))
        self.assertEqual(len(funnels), 1)


# ═══════════════════════════════════════════
# Retention Tests
# ═══════════════════════════════════════════

class RetentionServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=14, visitors_per_day=2)

    def test_retention_matrix(self):
        from apps.analytics.services.retention_service import RetentionService
        matrix = RetentionService.get_retention_matrix(
            website_id=str(self.website.id), num_weeks=4
        )
        self.assertIn("rows", matrix)
        self.assertIn("num_weeks", matrix)
        if matrix["rows"]:
            row = matrix["rows"][0]
            self.assertIn("cohort", row)
            self.assertIn("cohort_size", row)
            self.assertIn("weeks", row)
            # Week 0 retention should be 100%
            self.assertEqual(row["weeks"][0]["pct"], 100.0)

    def test_retention_curve(self):
        from apps.analytics.services.retention_service import RetentionService
        curve = RetentionService.get_retention_curve(
            website_id=str(self.website.id), num_weeks=4
        )
        self.assertIsInstance(curve, list)
        if curve:
            self.assertIn("week", curve[0])
            self.assertIn("avg_retention_pct", curve[0])


# ═══════════════════════════════════════════
# Flow Tests
# ═══════════════════════════════════════════

class FlowServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=7, visitors_per_day=3)

    def test_user_flows(self):
        from apps.analytics.services.flow_service import FlowService
        flows = FlowService.get_user_flows(website_id=str(self.website.id))
        self.assertIn("nodes", flows)
        self.assertIn("links", flows)
        self.assertIn("total_paths", flows)
        # Should have page-to-page transitions
        if flows["links"]:
            self.assertIn("source", flows["links"][0])
            self.assertIn("target", flows["links"][0])
            self.assertIn("value", flows["links"][0])

    def test_entry_pages(self):
        from apps.analytics.services.flow_service import FlowService
        entry = FlowService.get_entry_pages(website_id=str(self.website.id))
        self.assertIsInstance(entry, list)
        if entry:
            self.assertIn("page", entry[0])
            self.assertIn("count", entry[0])

    def test_exit_pages(self):
        from apps.analytics.services.flow_service import FlowService
        exit_ = FlowService.get_exit_pages(website_id=str(self.website.id))
        self.assertIsInstance(exit_, list)


# ═══════════════════════════════════════════
# AI Insights Tests
# ═══════════════════════════════════════════

class AIInsightsServiceTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=10, visitors_per_day=3)

    def test_generate_insights(self):
        from apps.analytics.services.ai_insights_service import AIInsightsService
        insights = AIInsightsService.generate_insights(website_id=str(self.website.id))
        self.assertIsInstance(insights, list)
        if insights:
            self.assertIn("type", insights[0])
            self.assertIn("title", insights[0])
            self.assertIn("description", insights[0])
            self.assertIn(insights[0]["type"], ["critical", "warning", "opportunity", "info"])

    def test_detect_anomalies(self):
        from apps.analytics.services.ai_insights_service import AIInsightsService
        anomalies = AIInsightsService.detect_anomalies(website_id=str(self.website.id))
        self.assertIsInstance(anomalies, list)

    def test_suggest_actions(self):
        from apps.analytics.services.ai_insights_service import AIInsightsService
        actions = AIInsightsService.suggest_actions(website_id=str(self.website.id))
        self.assertIsInstance(actions, list)
        if actions:
            self.assertIn("action", actions[0])
            self.assertIn("priority", actions[0])


# ═══════════════════════════════════════════
# API Endpoint Tests
# ═══════════════════════════════════════════

class AnalyticsAPITest(APITestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        seed_analytics_data(self.website, days=7, visitors_per_day=2)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.wid = str(self.website.id)

    def test_chart_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/chart/?period=7d")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)

    def test_devices_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/devices/")
        self.assertEqual(response.status_code, 200)

    def test_countries_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/countries/")
        self.assertEqual(response.status_code, 200)

    def test_funnels_crud(self):
        # Create
        response = self.client.post(f"/api/v1/analytics/{self.wid}/funnels/", {
            "name": "Outfi Signup",
            "steps": [
                {"name": "Home", "type": "url", "value": "/"},
                {"name": "Pricing", "type": "url", "value": "/pricing"},
            ],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        response.data["id"]

        # List
        response = self.client.get(f"/api/v1/analytics/{self.wid}/funnels/")
        self.assertEqual(response.status_code, 200)

    def test_retention_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/retention/?weeks=4")
        self.assertEqual(response.status_code, 200)
        self.assertIn("rows", response.data)

    def test_flows_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/flows/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("nodes", response.data)

    def test_entry_exit_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/entry-exit/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("entry_pages", response.data)

    def test_visitors_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/visitors/")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertGreater(len(response.data), 0)

    def test_insights_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/insights/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("insights", response.data)
        self.assertIn("actions", response.data)

    def test_live_events_endpoint(self):
        response = self.client.get(f"/api/v1/analytics/{self.wid}/live/")
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_blocked(self):
        """Ensure unauthenticated requests are rejected."""
        client = APIClient()  # No auth
        response = client.get(f"/api/v1/analytics/{self.wid}/chart/")
        self.assertIn(response.status_code, [401, 403])


# ═══════════════════════════════════════════
# Website Creation Tests
# ═══════════════════════════════════════════

class WebsiteCreationTest(APITestCase):
    def setUp(self):
        self.user = create_test_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_create_website(self):
        response = self.client.post("/api/v1/websites/", {
            "name": "Outfi Store",
            "url": "https://www.outfi.ai",
            "industry": "Fashion",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "Outfi Store")
        self.assertIn("pixel_key", response.data)

    def test_create_multiple_websites(self):
        """Project limits are disabled for testing — should allow multiple."""
        for i in range(5):
            response = self.client.post("/api/v1/websites/", {
                "name": f"Site {i}",
                "url": f"https://site{i}.com",
            })
            self.assertEqual(response.status_code, 201)
        self.assertEqual(Website.objects.filter(user=self.user).count(), 5)

    def test_create_website_invalid_url(self):
        response = self.client.post("/api/v1/websites/", {
            "name": "Bad Site",
            "url": "not-a-url",
        })
        self.assertIn(response.status_code, [400, 422])


# ═══════════════════════════════════════════
# Event Log endpoint tests
# ═══════════════════════════════════════════

class EventLogViewTest(TestCase):
    """Persisted, paginated event log surfaced on the SEO Analytics Events tab."""

    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make_event(self, *, ts=None, event_type="pageview", url="/", device="desktop", country="US"):
        ts = ts or timezone.now()
        visitor, _ = Visitor.objects.get_or_create(
            website=self.website,
            fingerprint_hash=f"fp-{ts.isoformat()}-{url}",
            defaults={"device_type": device, "geo_country": country, "browser": "Chrome"},
        )
        return PageEvent.objects.create(
            visitor=visitor, website=self.website,
            url=url, event_type=event_type, timestamp=ts,
        )

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get(f"/api/v1/analytics/{self.website.id}/event-log/")
        self.assertEqual(resp.status_code, 401)

    def test_paginates_and_includes_retention_meta(self):
        now = timezone.now()
        for i in range(5):
            self._make_event(ts=now - timedelta(minutes=i), url=f"/page-{i}")
        resp = self.client.get(
            f"/api/v1/analytics/{self.website.id}/event-log/", {"page_size": 2},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["retention_days"], 180)

    def test_filters_by_event_type_device_and_q(self):
        now = timezone.now()
        for i in range(4):
            self._make_event(
                ts=now - timedelta(minutes=i),
                event_type="pageview" if i % 2 == 0 else "click",
                url=f"/page-{i}",
                device="mobile" if i < 2 else "desktop",
            )
        base = f"/api/v1/analytics/{self.website.id}/event-log/"

        self.assertEqual(self.client.get(base, {"event_type": "click"}).json()["total"], 2)
        self.assertEqual(self.client.get(base, {"device": "mobile"}).json()["total"], 2)
        body = self.client.get(base, {"q": "page-3"}).json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["results"][0]["url"].endswith("page-3"))

    def test_clamps_to_retention_floor(self):
        now = timezone.now()
        self._make_event(ts=now, url="/recent")
        self._make_event(ts=now - timedelta(days=181), url="/ancient")
        body = self.client.get(
            f"/api/v1/analytics/{self.website.id}/event-log/",
        ).json()
        urls = [r["url"] for r in body["results"]]
        self.assertEqual(body["total"], 1)
        self.assertIn("/recent", urls)
        self.assertNotIn("/ancient", urls)

    def test_csv_export(self):
        self._make_event(url="/csv-target")
        resp = self.client.get(
            f"/api/v1/analytics/{self.website.id}/event-log/", {"format": "csv"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", resp["Content-Disposition"])
        body = b"".join(resp.streaming_content).decode()
        self.assertIn("/csv-target", body)
        self.assertIn("timestamp", body.splitlines()[0])


# ═══════════════════════════════════════════
# Ingestion accuracy tests (bot filter, visit count, sessions, timestamps)
# ═══════════════════════════════════════════

class IngestionAccuracyTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        from apps.analytics.services.event_ingestion_service import EventIngestionService
        self.svc = EventIngestionService

    def _req(self, ua="Mozilla/5.0 (Macintosh) Chrome/126.0 Safari/537", ip="203.0.113.5", country=None):
        r = RequestFactory().post("/api/v1/track/event/")
        r.META["HTTP_USER_AGENT"] = ua
        r.META["REMOTE_ADDR"] = ip
        if country:
            r.META["HTTP_CF_IPCOUNTRY"] = country
        return r

    def test_country_captured_from_edge_header(self):
        ev = self.svc.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data=self._event(),
            request=self._req(country="de"),
        )
        self.assertEqual(ev.visitor.geo_country, "DE")

    def test_country_backfilled_on_later_event(self):
        key = str(self.website.pixel_key)
        # First event has no resolvable country.
        self.svc.ingest_event(
            pixel_key=key, event_data=self._event(), request=self._req(),
        )
        v = Visitor.objects.get(website=self.website)
        self.assertEqual(v.geo_country, "")
        # A later event for the same visitor now carries an edge country.
        self.svc.ingest_event(
            pixel_key=key, event_data=self._event(url="https://www.outfi.ai/x"),
            request=self._req(country="ca"),
        )
        v.refresh_from_db()
        self.assertEqual(v.geo_country, "CA")

    def _event(self, **over):
        data = {"fingerprint": "fp-human-1", "url": "https://www.outfi.ai/", "event_type": "pageview"}
        data.update(over)
        return data

    def test_bot_events_are_dropped(self):
        for ua in [
            "Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            "Mozilla/5.0 (compatible; AhrefsBot/7.0)",
            "Mozilla/5.0 (compatible; Googlebot/2.1)",
            "curl/8.1.2",
        ]:
            out = self.svc.ingest_event(
                pixel_key=str(self.website.pixel_key),
                event_data=self._event(fingerprint=""),
                request=self._req(ua=ua),
            )
            self.assertIsNone(out, f"expected bot UA dropped: {ua}")
        self.assertEqual(Visitor.objects.filter(website=self.website).count(), 0)
        self.assertEqual(PageEvent.objects.filter(website=self.website).count(), 0)

    def test_human_event_is_kept(self):
        out = self.svc.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data=self._event(),
            request=self._req(),
        )
        self.assertIsNotNone(out)
        self.assertEqual(Visitor.objects.filter(website=self.website).count(), 1)

    def test_visit_count_increments_per_session_not_per_event(self):
        key = str(self.website.pixel_key)
        # Three events in the same session (within 30 min) -> visit_count stays 1.
        for url in ("/a", "/b", "/c"):
            self.svc.ingest_event(
                pixel_key=key,
                event_data=self._event(url="https://www.outfi.ai" + url),
                request=self._req(),
            )
        v = Visitor.objects.get(website=self.website)
        self.assertEqual(v.visit_count, 1)
        self.assertEqual(Session.objects.filter(visitor=v).count(), 1)

        # A 4th event 40 minutes later opens a new session -> visit_count 2.
        future = (timezone.now() + timedelta(minutes=40)).isoformat()
        self.svc.ingest_event(
            pixel_key=key,
            event_data=self._event(url="https://www.outfi.ai/d", timestamp=future),
            request=self._req(),
        )
        v.refresh_from_db()
        self.assertEqual(v.visit_count, 2)
        self.assertEqual(Session.objects.filter(visitor=v).count(), 2)

    def test_page_count_increments_atomically(self):
        key = str(self.website.pixel_key)
        for url in ("/a", "/b", "/c"):
            self.svc.ingest_event(
                pixel_key=key,
                event_data=self._event(url="https://www.outfi.ai" + url),
                request=self._req(),
            )
        session = Session.objects.get(visitor__website=self.website)
        self.assertEqual(session.page_count, 3)

    def test_session_end_event_closes_session(self):
        key = str(self.website.pixel_key)
        self.svc.ingest_event(pixel_key=key, event_data=self._event(), request=self._req())
        self.svc.ingest_event(
            pixel_key=key,
            event_data=self._event(event_type="session_end"),
            request=self._req(),
        )
        session = Session.objects.get(visitor__website=self.website)
        self.assertIsNotNone(session.ended_at)

    def test_absurd_future_timestamp_is_clamped(self):
        key = str(self.website.pixel_key)
        far_future = (timezone.now() + timedelta(days=30)).isoformat()
        ev = self.svc.ingest_event(
            pixel_key=key,
            event_data=self._event(timestamp=far_future),
            request=self._req(),
        )
        # Stored timestamp should be ~now, not 30 days out.
        self.assertLess((ev.timestamp - timezone.now()).total_seconds(), 60)

    def test_same_fingerprint_different_site_is_a_distinct_visitor(self):
        other = create_test_website(self.user, name="Other", url="https://other.example")
        self.svc.ingest_event(
            pixel_key=str(self.website.pixel_key),
            event_data=self._event(fingerprint="shared-fp"),
            request=self._req(),
        )
        self.svc.ingest_event(
            pixel_key=str(other.pixel_key),
            event_data={"fingerprint": "shared-fp", "url": "https://other.example/", "event_type": "pageview"},
            request=self._req(),
        )
        # Salting by website id means the same fingerprint hashes differently
        # per site -> one visitor each, no cross-site merge.
        self.assertEqual(Visitor.objects.filter(website=self.website).count(), 1)
        self.assertEqual(Visitor.objects.filter(website=other).count(), 1)
        hashes = set(Visitor.objects.values_list("fingerprint_hash", flat=True))
        self.assertEqual(len(hashes), 2)


# ═══════════════════════════════════════════
# Ingestion input sanitization (security)
# ═══════════════════════════════════════════

class IngestionSanitizationTest(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)
        from apps.analytics.services.event_ingestion_service import EventIngestionService
        self.svc = EventIngestionService

    def _req(self):
        r = RequestFactory().post("/api/v1/track/event/")
        r.META["HTTP_USER_AGENT"] = _HUMAN_UA
        r.META["REMOTE_ADDR"] = "203.0.113.5"
        return r

    def _ingest(self, **over):
        data = {"fingerprint": "fp-1", "url": "https://www.outfi.ai/", "event_type": "pageview"}
        data.update(over)
        return self.svc.ingest_event(
            pixel_key=str(self.website.pixel_key), event_data=data, request=self._req(),
        )

    def test_unknown_event_type_becomes_custom(self):
        ev = self._ingest(event_type="'; DROP TABLE analytics_pageevent; --")
        self.assertEqual(ev.event_type, "custom")

    def test_event_name_truncated(self):
        ev = self._ingest(event_name="A" * 500)
        self.assertLessEqual(len(ev.event_name), 100)

    def test_url_truncated(self):
        ev = self._ingest(url="https://www.outfi.ai/" + "p" * 5000)
        self.assertLessEqual(len(ev.url), 2000)

    def test_properties_key_count_capped(self):
        ev = self._ingest(properties={f"k{i}": i for i in range(200)})
        self.assertLessEqual(len(ev.properties), 30)

    def test_properties_value_truncated_and_nesting_dropped(self):
        ev = self._ingest(properties={
            "long": "x" * 2000,
            "nested": {"evil": "payload"},
            "ok": "fine",
        })
        self.assertLessEqual(len(ev.properties["long"]), 500)
        self.assertNotIn("nested", ev.properties)
        self.assertEqual(ev.properties["ok"], "fine")

    def test_properties_non_dict_dropped(self):
        ev = self._ingest(properties="not-a-dict")
        self.assertEqual(ev.properties, {})

    def test_scroll_depth_clamped(self):
        ev = self._ingest(scroll_depth=9999)
        self.assertEqual(ev.scroll_depth, 100)

    def test_time_on_page_rejects_negative(self):
        ev = self._ingest(time_on_page_ms=-5000)
        self.assertIsNone(ev.time_on_page_ms)


class IngestEndpointBodySizeTest(APITestCase):
    def setUp(self):
        self.user = create_test_user()
        self.website = create_test_website(self.user)

    def test_oversized_body_rejected_413(self):
        # Declare a huge content-length; the guard rejects before parsing.
        resp = self.client.post(
            "/api/v1/track/event/",
            data="{}",
            content_type="application/json",
            HTTP_CONTENT_LENGTH=str(200 * 1024),
            HTTP_ORIGIN="https://www.outfi.ai",
        )
        self.assertEqual(resp.status_code, 413)
