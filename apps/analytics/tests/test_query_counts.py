"""Query-count budgets for the analytics read paths.

These endpoints are re-rendered on every dashboard load, so their cost is
paid constantly. The assertions below are exact on purpose: a change that
adds a query should fail here and be a deliberate decision, not something
that shows up months later as a slow dashboard.

The most important test in this file is
``test_engagement_query_count_is_flat_in_returner_count`` - it pins the
N+1 that used to run one Avg aggregate per returning visitor.
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import PageEvent, Session, Visitor
from apps.analytics.services.analytics_service import AnalyticsService
from apps.analytics.services.retention_service import RetentionService
from apps.websites.models import Website


def _make_website():
    from apps.accounts.models import User

    user = User.objects.create_user(
        email=f"qc-{uuid.uuid4().hex[:8]}@cansee.ai",
        password="TestPass123!",
        full_name="Query Count",
    )
    return Website.objects.create(
        user=user,
        name="QC",
        url="https://qc.example.com",
        industry="Fashion",
        pixel_key=uuid.uuid4(),
        is_active=True,
    )


def _make_returning_visitors(website, count, start_index=0):
    """Create ``count`` visitors with visit_count > 1 and one session each.

    first_seen/last_seen are auto_now_add/auto_now, so they have to be
    written with .update() after the row exists.

    ``start_index`` offsets the fingerprint so a second call can add more
    visitors to the same website. Visitor is unique_together on
    (website, fingerprint_hash), so restarting at 0 would collide.
    """
    now = timezone.now()
    for i in range(start_index, start_index + count):
        visitor = Visitor.objects.create(
            website=website,
            fingerprint_hash=f"returner_{i}",
            geo_country="US",
            device_type="desktop",
            browser="Chrome",
            os="MacOS",
            visit_count=i + 2,
        )
        Visitor.objects.filter(pk=visitor.pk).update(
            first_seen=now - timedelta(days=60),
            last_seen=now - timedelta(days=1),
        )
        started = now - timedelta(days=1)
        Session.objects.create(
            visitor=visitor,
            started_at=started,
            ended_at=started + timedelta(minutes=4),
            page_count=3,
            entry_page="https://qc.example.com/",
            exit_page="https://qc.example.com/out",
            source="google",
            medium="organic",
        )
        PageEvent.objects.create(
            visitor=visitor,
            website=website,
            url="https://qc.example.com/",
            event_type="pageview",
            timestamp=started,
        )


class TestOverviewQueryBudget(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.website = _make_website()
        _make_returning_visitors(cls.website, 5)

    def test_overview_costs_two_queries(self):
        # One filtered aggregate over Visitor covering both the current and
        # previous window, plus one COUNT on PageEvent. Three separate
        # Visitor COUNTs collapsed into the first.
        with self.assertNumQueries(2):
            AnalyticsService.get_overview(website_id=str(self.website.id))

    def test_overview_returns_the_same_shape(self):
        data = AnalyticsService.get_overview(website_id=str(self.website.id))
        assert set(data) == {
            "period", "total_visitors", "total_pageviews",
            "hot_leads", "visitor_growth_pct",
        }


class TestEngagementQueryBudget(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.website = _make_website()
        _make_returning_visitors(cls.website, 4)

    def test_engagement_costs_three_queries(self):
        # 1. filtered aggregate over Visitor (total/new/returning/multi)
        # 2. filtered aggregate over Session (count/bounced/avg pages/avg duration)
        # 3. top returners, with avg pages annotated rather than looked up
        with self.assertNumQueries(3):
            RetentionService.get_engagement_metrics(website_id=str(self.website.id))

    def test_engagement_query_count_is_flat_in_returner_count(self):
        """The regression guard. This is why the file exists.

        The old implementation ran one Avg aggregate per returning visitor,
        so this count grew with the data. If someone reintroduces a query
        inside that loop, the 12-returner case fails while the 4-returner
        case still passes - which is exactly the signature of an N+1 and
        exactly what makes it easy to ship unnoticed.
        """
        with self.assertNumQueries(3):
            RetentionService.get_engagement_metrics(website_id=str(self.website.id))

        # Triple the returner count. The old implementation would now issue
        # 12 extra queries; the annotated version issues none.
        _make_returning_visitors(self.website, 12, start_index=100)
        assert Visitor.objects.filter(
            website=self.website, visit_count__gt=1,
        ).count() == 16

        with self.assertNumQueries(3):
            RetentionService.get_engagement_metrics(website_id=str(self.website.id))

    def test_avg_duration_is_computed_over_every_session(self):
        """The old loop sliced [:200] with no ordering, so it averaged an
        arbitrary sample and labelled it the period average. Every seeded
        session is exactly 4 minutes, so the answer must be 240 regardless
        of how many there are."""
        data = RetentionService.get_engagement_metrics(website_id=str(self.website.id))
        assert data["avg_session_duration_secs"] == 240

    def test_top_returners_still_carries_avg_pages(self):
        data = RetentionService.get_engagement_metrics(website_id=str(self.website.id))
        assert data["top_returners"], "expected returners in the fixture"
        for row in data["top_returners"]:
            assert row["avg_pages"] == 3.0
            assert set(row) == {
                "hash", "visits", "last_seen", "device", "country",
                "browser", "avg_pages",
            }
