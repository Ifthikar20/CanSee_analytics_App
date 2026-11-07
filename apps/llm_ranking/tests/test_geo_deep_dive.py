"""Tests for the dashboard deep-dive analytics."""
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.llm_ranking.services.geo_deep_dive import (
    MAX_PROMPTS,
    build_for_user,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _completed_audit(user, **kwargs):
    return LLMRankingAuditFactory(
        created_by=user,
        status=LLMRankingAudit.STATUS_COMPLETED,
        completed_at=timezone.now(),
        **kwargs,
    )


@pytest.mark.django_db
class TestBuildForUser:
    def test_returns_none_when_no_audits(self):
        user = UserFactory()
        assert build_for_user(user) is None

    def test_visibility_matrix_dedupes_by_most_recent_audit(self):
        user = UserFactory()
        older = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at=timezone.now() - timezone.timedelta(days=5),
        )
        newer = LLMRankingAuditFactory(
            created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at=timezone.now(),
        )
        LLMRankingResultFactory(
            audit=older, prompt="P1", provider="claude",
            query_succeeded=True, is_mentioned=False, mention_rank=None,
        )
        LLMRankingResultFactory(
            audit=newer, prompt="P1", provider="claude",
            query_succeeded=True, is_mentioned=True, mention_rank=2,
        )
        with patch("apps.llm_ranking.providers.get_provider", return_value=None):
            matrix = build_for_user(user)["visibility_matrix"]
        row = next(r for r in matrix["prompts"] if r["prompt"] == "P1")
        assert row["cells"][matrix["providers"].index("claude")] == {
            "is_mentioned": True, "mention_rank": 2,
        }

    def test_position_buckets_split_per_provider(self):
        user = UserFactory()
        audit = _completed_audit(user)
        for provider, rank in [
            ("claude", 1), ("claude", 1), ("claude", 5),
            ("gpt4", 12), ("gpt4", 2),
        ]:
            LLMRankingResultFactory(
                audit=audit, provider=provider,
                query_succeeded=True, is_mentioned=True, mention_rank=rank,
            )
        out = build_for_user(user)["position_by_provider"]
        claude = next(p for p in out if p["provider"] == "claude")
        gpt4 = next(p for p in out if p["provider"] == "gpt4")
        claude_buckets = {b["range"]: b["count"] for b in claude["buckets"]}
        gpt4_buckets = {b["range"]: b["count"] for b in gpt4["buckets"]}
        assert claude_buckets == {"1": 2, "2-3": 0, "4-10": 1, "11+": 0}
        assert gpt4_buckets == {"1": 0, "2-3": 1, "4-10": 0, "11+": 1}

    def test_sentiment_split_per_provider(self):
        user = UserFactory()
        audit = _completed_audit(user)
        LLMRankingResultFactory(
            audit=audit, provider="claude", query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
        )
        LLMRankingResultFactory(
            audit=audit, provider="claude", query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_NEGATIVE,
        )
        LLMRankingResultFactory(
            audit=audit, provider="gpt4", query_succeeded=True, is_mentioned=True,
            sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
        )
        out = build_for_user(user)["sentiment_by_provider"]
        claude = {s["sentiment"]: s["count"] for s in next(p for p in out if p["provider"] == "claude")["split"]}
        gpt4 = {s["sentiment"]: s["count"] for s in next(p for p in out if p["provider"] == "gpt4")["split"]}
        assert claude == {"positive": 1, "neutral": 0, "negative": 1}
        assert gpt4 == {"positive": 1, "neutral": 0, "negative": 0}

    def test_themes_skipped_when_provider_not_configured(self):
        user = UserFactory()
        audit = _completed_audit(user)
        for _ in range(6):
            LLMRankingResultFactory(
                audit=audit, query_succeeded=True, is_mentioned=True,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
                mention_context="great onboarding flow", confidence_score=90,
            )
        with patch(
            "apps.llm_ranking.providers.get_provider",
            return_value=None,
        ):
            themes = build_for_user(user)["sentiment_themes"]
        assert themes == {"positive": [], "negative": []}

    def test_themes_parsed_from_provider_response(self):
        user = UserFactory()
        audit = _completed_audit(user)
        for _ in range(6):
            LLMRankingResultFactory(
                audit=audit, query_succeeded=True, is_mentioned=True,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
                mention_context="great onboarding flow", confidence_score=90,
            )
        fake_result = MagicMock(
            succeeded=True,
            text='[{"label": "Smooth onboarding", "count": 6, "sample": "great onboarding flow"}]',
            error="",
        )
        fake_provider = MagicMock(is_configured=MagicMock(return_value=True))
        fake_provider.query.return_value = fake_result
        with patch(
            "apps.llm_ranking.providers.get_provider",
            return_value=fake_provider,
        ):
            themes = build_for_user(user)["sentiment_themes"]
        assert themes["positive"] == [
            {"label": "Smooth onboarding", "count": 6, "sample_quote": "great onboarding flow"},
        ]

    def test_themes_disabled_by_setting(self, settings):
        settings.GEO_SENTIMENT_THEMES_ENABLED = False
        user = UserFactory()
        audit = _completed_audit(user)
        for _ in range(6):
            LLMRankingResultFactory(
                audit=audit, query_succeeded=True, is_mentioned=True,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
                mention_context="great onboarding flow", confidence_score=90,
            )
        themes = build_for_user(user)["sentiment_themes"]
        assert themes == {"positive": [], "negative": []}

    def test_prompts_filter_narrows_matrix(self):
        user = UserFactory()
        audit = _completed_audit(user)
        LLMRankingResultFactory(
            audit=audit, prompt="A", provider="claude",
            query_succeeded=True, is_mentioned=True, mention_rank=1,
        )
        LLMRankingResultFactory(
            audit=audit, prompt="B", provider="claude",
            query_succeeded=True, is_mentioned=False,
        )
        with patch("apps.llm_ranking.providers.get_provider", return_value=None):
            matrix = build_for_user(user, prompts=["A"])["visibility_matrix"]
        prompts_seen = [r["prompt"] for r in matrix["prompts"]]
        assert prompts_seen == ["A"]

    def test_available_prompts_ranked_by_volume(self):
        user = UserFactory()
        audit = _completed_audit(user)
        for _ in range(3):
            LLMRankingResultFactory(audit=audit, prompt="popular", query_succeeded=True)
        LLMRankingResultFactory(audit=audit, prompt="rare", query_succeeded=True)
        with patch("apps.llm_ranking.providers.get_provider", return_value=None):
            available = build_for_user(user)["available_prompts"]
        labels = [p["label"] for p in available]
        assert labels[0] == "popular"
        assert "rare" in labels

    def test_visibility_matrix_caps_at_max_prompts(self):
        user = UserFactory()
        audit = _completed_audit(user)
        for i in range(MAX_PROMPTS + 5):
            LLMRankingResultFactory(
                audit=audit, prompt=f"prompt-{i}", provider="claude",
                query_succeeded=True, is_mentioned=True, mention_rank=1,
            )
        with patch(
            "apps.llm_ranking.providers.get_provider",
            return_value=None,
        ):
            matrix = build_for_user(user)["visibility_matrix"]
        assert len(matrix["prompts"]) == MAX_PROMPTS
