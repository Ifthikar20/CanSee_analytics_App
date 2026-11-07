"""Tests for the GEO KPI builder used by the dashboard Analytics tab."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.llm_ranking.services.geo_stats import (
    PERIOD_DAYS,
    build_breakdowns_for_user,
    build_kpis_for_user,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)


def _by_label(tiles, label):
    return next(t for t in tiles if t["label"] == label)


@pytest.mark.django_db
class TestBuildKpisForUser:
    def test_returns_none_when_user_has_no_completed_audits(self):
        user = UserFactory()
        LLMRankingAuditFactory(created_by=user, status=LLMRankingAudit.STATUS_PENDING)
        assert build_kpis_for_user(user) is None

    def test_returns_three_tiles_in_order(self):
        user = UserFactory()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=42.0, avg_mention_rank=3.0, completed_at=timezone.now(),
        )
        tiles = build_kpis_for_user(user)
        assert [t["label"] for t in tiles] == ["Visibility", "Position", "Sentiment"]

    def test_visibility_value_reflects_current_period(self):
        user = UserFactory()
        now = timezone.now()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=60.0, avg_mention_rank=2.0, completed_at=now,
        )
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=40.0, avg_mention_rank=4.0, completed_at=now,
        )
        tiles = build_kpis_for_user(user)
        assert _by_label(tiles, "Visibility")["value"] == "50.0%"
        assert _by_label(tiles, "Position")["value"] == "3.0"

    def test_position_direction_inverts_higher_is_worse(self):
        """A lower avg rank vs. the previous period is an 'up' trend."""
        user = UserFactory()
        now = timezone.now()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=20.0, avg_mention_rank=5.0,
            completed_at=now - timedelta(days=PERIOD_DAYS + 5),
        )
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=20.0, avg_mention_rank=2.0, completed_at=now,
        )
        tile = _by_label(build_kpis_for_user(user), "Position")
        assert tile["direction"] == "up"
        assert tile["trendNote"] == "Trending up"

    def test_sentiment_score_from_mention_distribution(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=80.0, completed_at=timezone.now(),
        )
        for _ in range(3):
            LLMRankingResultFactory(
                audit=audit, query_succeeded=True, is_mentioned=True,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
            )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_NEGATIVE,
        )
        # 3 positive, 1 negative, 4 total -> 50 + 50 * (3-1)/4 = 75
        assert _by_label(build_kpis_for_user(user), "Sentiment")["value"] == "75"

    def test_missing_previous_period_yields_neutral_change(self):
        user = UserFactory()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=42.0, avg_mention_rank=3.0, completed_at=timezone.now(),
        )
        tile = _by_label(build_kpis_for_user(user), "Visibility")
        assert tile["change"] == "—"
        assert tile["trendNote"] == "No prior period to compare"

    def test_other_users_audits_excluded(self):
        user = UserFactory()
        stranger = UserFactory()
        LLMRankingAuditFactory(
            created_by=stranger, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=99.0, completed_at=timezone.now(),
        )
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=10.0, completed_at=timezone.now(),
        )
        tile = _by_label(build_kpis_for_user(user), "Visibility")
        assert tile["value"] == "10.0%"


@pytest.mark.django_db
class TestFilters:
    def test_prompts_filter_narrows_visibility(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=99.0, completed_at=timezone.now(),
        )
        LLMRankingResultFactory(audit=audit, prompt="A", query_succeeded=True, is_mentioned=True)
        LLMRankingResultFactory(audit=audit, prompt="A", query_succeeded=True, is_mentioned=False)
        LLMRankingResultFactory(audit=audit, prompt="B", query_succeeded=True, is_mentioned=True)
        LLMRankingResultFactory(audit=audit, prompt="B", query_succeeded=True, is_mentioned=True)
        tile = _by_label(build_kpis_for_user(user, prompts=["A"]), "Visibility")
        # Only A's two cells: 1 mention out of 2 -> 50%
        assert tile["value"] == "50.0%"

    def test_explicit_start_end_window(self):
        user = UserFactory()
        now = timezone.now()
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=80.0, completed_at=now - timedelta(days=2),
        )
        LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=20.0, completed_at=now - timedelta(days=60),
        )
        tile = _by_label(
            build_kpis_for_user(user, start=now - timedelta(days=7), end=now),
            "Visibility",
        )
        # Only the recent 80% audit lands in the requested window.
        assert tile["value"] == "80.0%"


@pytest.mark.django_db
class TestBuildBreakdownsForUser:
    def test_returns_none_with_no_audits(self):
        user = UserFactory()
        assert build_breakdowns_for_user(user) is None

    def test_visibility_by_provider(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at=timezone.now(),
        )
        LLMRankingResultFactory(
            audit=audit, provider="claude", query_succeeded=True, is_mentioned=True,
        )
        LLMRankingResultFactory(
            audit=audit, provider="claude", query_succeeded=True, is_mentioned=False,
        )
        LLMRankingResultFactory(
            audit=audit, provider="gpt4", query_succeeded=True, is_mentioned=True,
        )
        # Failed cells are excluded.
        LLMRankingResultFactory(
            audit=audit, provider="claude", query_succeeded=False, is_mentioned=True,
        )
        by_provider = build_breakdowns_for_user(user)["visibility"]["by_provider"]
        claude = next(r for r in by_provider if r["provider"] == "claude")
        gpt4 = next(r for r in by_provider if r["provider"] == "gpt4")
        assert claude == {"provider": "claude", "mentions": 1, "total": 2, "mention_rate": 50.0}
        assert gpt4 == {"provider": "gpt4", "mentions": 1, "total": 1, "mention_rate": 100.0}

    def test_position_distribution_buckets(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at=timezone.now(),
        )
        for rank in (1, 1, 2, 3, 5, 12):
            LLMRankingResultFactory(
                audit=audit, query_succeeded=True, is_mentioned=True, mention_rank=rank,
            )
        dist = build_breakdowns_for_user(user)["position"]["distribution"]
        counts = {row["range"]: row["count"] for row in dist}
        assert counts == {"1": 2, "2-3": 2, "4-10": 1, "11+": 1}

    def test_sentiment_split_and_samples(self):
        user = UserFactory()
        audit = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at=timezone.now(),
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
            mention_context="Acme is the best option", confidence_score=95.0,
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_NEGATIVE,
            mention_context="Acme has poor reliability", confidence_score=88.0,
        )
        result = build_breakdowns_for_user(user)["sentiment"]
        split = {row["sentiment"]: row["count"] for row in result["split"]}
        assert split == {"positive": 1, "neutral": 0, "negative": 1}
        sentiments = {s["sentiment"] for s in result["samples"]}
        assert sentiments == {"positive", "negative"}
