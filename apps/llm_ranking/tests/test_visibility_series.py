"""Tests for the visibility-over-time series builder."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit
from apps.llm_ranking.services.visibility_series import (
    DAY_BUCKETS,
    MONTH12_BUCKETS,
    MONTH_BUCKETS,
    WEEK_BUCKETS,
    build_for_user,
    build_month12_for_website,
    build_overview_for_website,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.websites.tests.factories import WebsiteFactory


@pytest.mark.django_db
class TestBuildForUser:
    def test_returns_none_when_user_has_no_completed_audits(self):
        user = UserFactory()
        # A pending audit must not flip the gate.
        LLMRankingAuditFactory(created_by=user, status=LLMRankingAudit.STATUS_PENDING)
        assert build_for_user(user) is None

    def test_returns_all_three_resolutions_when_user_has_data(self):
        user = UserFactory()
        LLMRankingAuditFactory(
            created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=42.0,
            completed_at=timezone.now(),
        )
        series = build_for_user(user)
        assert set(series) == {"day", "week", "month", "month12"}
        assert len(series["day"]["labels"]) == DAY_BUCKETS
        assert len(series["week"]["labels"]) == WEEK_BUCKETS
        assert len(series["month"]["labels"]) == MONTH_BUCKETS
        assert len(series["month12"]["labels"]) == MONTH12_BUCKETS
        for res in ("day", "week", "month", "month12"):
            assert len(series[res]["brand"]) == len(series[res]["labels"])
            assert len(series[res]["competitor"]) == len(series[res]["labels"])

    def test_brand_series_averages_mention_rate_in_bucket(self):
        user = UserFactory()
        now = timezone.now()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=40.0, completed_at=now,
        )
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=60.0, completed_at=now,
        )
        series = build_for_user(user)
        # Today's bucket is the last entry in the day series.
        assert series["day"]["brand"][-1] == 50.0

    def test_competitor_rate_is_share_of_answers_with_competitors(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=10.0, completed_at=timezone.now(),
        )
        LLMRankingResultFactory(audit=audit, query_succeeded=True, competitors_mentioned=[])
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True, competitors_mentioned=["Rival"],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=[{"name": "Rival2", "position": 2}],
        )
        # Failed cell should be ignored entirely.
        LLMRankingResultFactory(
            audit=audit, query_succeeded=False, competitors_mentioned=["Ignored"],
        )
        series = build_for_user(user)
        # 2 of 3 succeeded cells mention a competitor -> 66.7%
        assert series["day"]["competitor"][-1] == pytest.approx(66.7, abs=0.05)

    def test_other_users_audits_are_excluded(self):
        user = UserFactory()
        stranger = UserFactory()
        LLMRankingAuditFactory(
            created_by=stranger, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=99.0, completed_at=timezone.now(),
        )
        # The user themselves has at least one audit so we get a series back.
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=10.0, completed_at=timezone.now() - timedelta(days=3),
        )
        series = build_for_user(user)
        # Today's bucket has no audits for this user -> 0.
        assert series["day"]["brand"][-1] == 0.0

    def test_empty_buckets_report_zero(self):
        user = UserFactory()
        # Only one audit, well in the past — month series should have a non-zero
        # entry only in the matching bucket.
        old = timezone.now() - timedelta(days=120)
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=30.0, completed_at=old,
        )
        series = build_for_user(user)
        assert series["day"]["brand"] == [0.0] * DAY_BUCKETS


@pytest.mark.django_db
class TestWebsiteScopedMonth12:
    def test_returns_twelve_buckets(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        out = build_month12_for_website(user, website)
        assert len(out["labels"]) == MONTH12_BUCKETS
        assert len(out["brand"]) == MONTH12_BUCKETS
        assert len(out["competitor"]) == MONTH12_BUCKETS
        # Empty database -> all zeros, not None.
        assert out["brand"] == [0.0] * MONTH12_BUCKETS
        assert out["competitor"] == [0.0] * MONTH12_BUCKETS

    def test_excludes_audits_from_other_websites(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        other = WebsiteFactory(user=user)
        LLMRankingAuditFactory(
            created_by=user, website=other,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=80.0, completed_at=timezone.now(),
        )
        out = build_month12_for_website(user, website)
        assert out["brand"][-1] == 0.0


@pytest.mark.django_db
class TestBuildOverview:
    def test_empty_state(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        out = build_overview_for_website(user, website)
        assert out["has_data"] is False
        assert out["brand_current"] == 0.0
        assert out["competitor_current"] == 0.0
        assert out["brand_delta_pct"] == 0.0
        assert len(out["labels"]) == MONTH12_BUCKETS

    def test_headline_is_most_recent_populated_bucket(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        LLMRankingAuditFactory(
            created_by=user, website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=42.0, completed_at=timezone.now(),
        )
        out = build_overview_for_website(user, website)
        assert out["has_data"] is True
        assert out["brand_current"] == 42.0

    def test_trend_delta_uses_three_month_windows(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        now = timezone.now()
        # Prior 3-mo mean ~ 10, recent 3-mo mean ~ 50 -> +400%.
        for months_ago, rate in [
            (5, 10.0), (4, 10.0), (3, 10.0),
            (2, 50.0), (1, 50.0), (0, 50.0),
        ]:
            LLMRankingAuditFactory(
                created_by=user, website=website,
                status=LLMRankingAudit.STATUS_COMPLETED,
                mention_rate=rate,
                completed_at=now - timedelta(days=months_ago * 30 + 1),
            )
        out = build_overview_for_website(user, website)
        assert out["brand_delta_pct"] == pytest.approx(400.0, abs=1.0)
        assert out["brand_current"] == 50.0

    def test_per_competitor_breakdown_ranks_by_total_mentions(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(
            created_by=user, website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=50.0, completed_at=timezone.now(),
        )
        # Four successful answers: Mixpanel in 3, Amplitude in 2, Heap in 1.
        # Expected rates: Mixpanel 75%, Amplitude 50%, Heap 25%.
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=[{"name": "Mixpanel"}, {"name": "Amplitude"}],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=["Mixpanel", "Heap"],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=["Mixpanel", "Amplitude"],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=[],
        )

        out = build_overview_for_website(user, website)
        names = [c["name"] for c in out["competitors"]]
        assert names[:3] == ["Mixpanel", "Amplitude", "Heap"]

        # Per-competitor current (last populated bucket) reflects the rates.
        by_name = {c["name"]: c for c in out["competitors"]}
        assert by_name["Mixpanel"]["current"] == 75.0
        assert by_name["Amplitude"]["current"] == 50.0
        assert by_name["Heap"]["current"] == 25.0

        # Competitor avg = mean across the three rates = 50.0.
        assert out["competitor_current"] == 50.0

    def test_competitor_names_are_deduped_case_insensitively(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(
            created_by=user, website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=10.0, completed_at=timezone.now(),
        )
        # Same answer names "Mixpanel" twice in different casings — should
        # only count once.
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=["Mixpanel", "mixpanel"],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=[],
        )
        out = build_overview_for_website(user, website)
        # One competitor, mentioned in 1 of 2 answers -> 50%.
        assert len(out["competitors"]) == 1
        assert out["competitors"][0]["current"] == 50.0

    def test_empty_state_returns_empty_competitor_list(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        out = build_overview_for_website(user, website)
        assert out["competitors"] == []
        assert out["competitor_current"] == 0.0
