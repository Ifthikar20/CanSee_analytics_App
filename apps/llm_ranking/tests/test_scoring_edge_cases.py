"""
Edge cases for ``compute_overall_score`` and related scoring helpers.

The happy paths are covered in ``test_services.py``. This file exercises
boundary conditions: zero queries, all failures, all unanimous, single
provider, mixed sentiment, and the Beta-Binomial / Wilson interactions.
"""
import pytest

from apps.llm_ranking.models import LLMRankingResult
from apps.llm_ranking.services.ranking_service import (
    LLMRankingService,
    beta_binomial_mean,
    wilson_ci,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)


@pytest.mark.django_db
class TestComputeOverallScoreEdgeCases:
    def test_no_results_returns_zeroed_metrics(self):
        scores = LLMRankingService.compute_overall_score([])
        assert scores["overall_score"] == 0
        assert scores["mention_rate"] == 0.0
        assert scores["mention_rate_smoothed"] == 0.0
        assert scores["avg_mention_rank"] == 0.0
        assert scores["mention_rate_ci_lower"] == 0.0
        assert scores["mention_rate_ci_upper"] == 0.0

    def test_all_failed_queries_dont_crash(self):
        audit = LLMRankingAuditFactory()
        results = [
            LLMRankingResultFactory(
                audit=audit, provider="claude",
                query_succeeded=False, is_mentioned=False,
                sentiment=LLMRankingResult.SENTIMENT_NOT_MENTIONED,
                mention_rank=None,
            )
            for _ in range(4)
        ]
        scores = LLMRankingService.compute_overall_score(results)
        assert scores["mention_rate"] == 0.0
        assert scores["mention_rate_smoothed"] >= 0.0
        # No succeeded calls means no provider coverage bonus.
        assert scores["overall_score"] == 0

    def test_unanimous_top_rank_caps_at_100(self):
        audit = LLMRankingAuditFactory()
        results = []
        for prov in ("claude", "gpt4", "gemini", "perplexity"):
            for _ in range(5):
                results.append(LLMRankingResultFactory(
                    audit=audit, provider=prov,
                    query_succeeded=True, is_mentioned=True, mention_rank=1,
                    sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
                ))
        scores = LLMRankingService.compute_overall_score(results)
        assert scores["overall_score"] <= 100
        # 20 mentions / 20 queries → smoothed ≈ (2+20)/(10+20) = 73%, but
        # the score is bounded at 100 and the rank/sentiment/coverage bonuses
        # all max out — so the total should be at the cap.
        assert scores["overall_score"] >= 90
        # The Wilson CI on a unanimous proportion has lower < 1 ≤ upper = 100.
        assert scores["mention_rate_ci_upper"] == 100.0
        assert 0 < scores["mention_rate_ci_lower"] < 100

    def test_mixed_sentiment_yields_partial_score(self):
        audit = LLMRankingAuditFactory()
        # 2 positive, 2 neutral, 2 negative — all mentioned, all rank 1.
        sentiments = (
            [LLMRankingResult.SENTIMENT_POSITIVE] * 2
            + [LLMRankingResult.SENTIMENT_NEUTRAL] * 2
            + [LLMRankingResult.SENTIMENT_NEGATIVE] * 2
        )
        results = [
            LLMRankingResultFactory(
                audit=audit, provider="claude",
                query_succeeded=True, is_mentioned=True, mention_rank=1,
                sentiment=s,
            )
            for s in sentiments
        ]
        scores = LLMRankingService.compute_overall_score(results)
        # Sentiment component: (2*20 + 2*10 + 2*0) / 6 = 10 — out of 20.
        # Plus 100% mention rate (smoothed lower) * 0.4 + rank #1 = 30 +
        # provider coverage partial (1 provider).
        assert 0 < scores["overall_score"] < 90

    def test_only_one_provider_succeeded_caps_coverage(self):
        audit = LLMRankingAuditFactory()
        results = [
            LLMRankingResultFactory(
                audit=audit, provider="claude",
                query_succeeded=True, is_mentioned=True, mention_rank=1,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
            )
            for _ in range(5)
        ]
        scores = LLMRankingService.compute_overall_score(results)
        # Single provider: coverage bonus is 1/3 * 10 ≈ 3.3 pts, not 10.
        # We test indirectly: a 4-provider unanimous run scores higher.
        audit2 = LLMRankingAuditFactory()
        full_coverage = []
        for prov in ("claude", "gpt4", "gemini", "perplexity"):
            for _ in range(5):
                full_coverage.append(LLMRankingResultFactory(
                    audit=audit2, provider=prov,
                    query_succeeded=True, is_mentioned=True, mention_rank=1,
                    sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
                ))
        full_scores = LLMRankingService.compute_overall_score(full_coverage)
        assert full_scores["overall_score"] >= scores["overall_score"]

    def test_smoothed_pulls_small_samples_toward_prior(self):
        """1/1 should not score the same as 100/100 — smoothing differentiates."""
        audit = LLMRankingAuditFactory()
        small = [LLMRankingResultFactory(
            audit=audit, provider="claude",
            query_succeeded=True, is_mentioned=True, mention_rank=1,
            sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
        )]
        big = [LLMRankingResultFactory(
            audit=audit, provider="claude",
            query_succeeded=True, is_mentioned=True, mention_rank=1,
            sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
        ) for _ in range(50)]
        small_scores = LLMRankingService.compute_overall_score(small)
        big_scores = LLMRankingService.compute_overall_score(big)
        # Both have raw 100% mention rate, but the smoothed value differs.
        assert small_scores["mention_rate"] == 100.0
        assert big_scores["mention_rate"] == 100.0
        assert small_scores["mention_rate_smoothed"] < big_scores["mention_rate_smoothed"]

    def test_partial_rank_data_uses_only_known_ranks(self):
        """Mentions without a rank don't poison the avg_rank calculation."""
        audit = LLMRankingAuditFactory()
        results = [
            LLMRankingResultFactory(
                audit=audit, provider="claude",
                query_succeeded=True, is_mentioned=True, mention_rank=1,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
            ),
            LLMRankingResultFactory(
                audit=audit, provider="gpt4",
                query_succeeded=True, is_mentioned=True, mention_rank=None,
                sentiment=LLMRankingResult.SENTIMENT_POSITIVE,
            ),
        ]
        scores = LLMRankingService.compute_overall_score(results)
        assert scores["avg_mention_rank"] == 1.0


class TestBetaBinomialBoundaries:
    def test_zero_n_yields_prior_mean(self):
        # Beta(2,8) prior mean is 0.2.
        assert abs(beta_binomial_mean(0, 0) - 0.2) < 1e-9

    def test_extreme_skew_sums_to_one_with_complement(self):
        # Conjugate prior makes successes and failures symmetric:
        # mean(s,n) + mean(n-s,n) under the SAME prior should sum to:
        #     (alpha + beta) / (alpha + beta + n) * 1
        # ... but we use the same prior on both sides, so it sums to 1.
        # Use a symmetric Beta(5,5) for this property.
        s_mean = beta_binomial_mean(7, 10, alpha=5, beta=5)
        c_mean = beta_binomial_mean(3, 10, alpha=5, beta=5)
        assert abs(s_mean + c_mean - 1.0) < 1e-9

    def test_negative_n_clipped_to_zero(self):
        # Defensive — never throw on weird inputs.
        # 0/0 should give the prior mean.
        assert abs(beta_binomial_mean(0, -5) - 0.2) < 1e-9


class TestWilsonCIBoundaries:
    def test_zero_n_returns_zero_zero(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)

    def test_full_proportion_upper_bound_is_one(self):
        lower, upper = wilson_ci(20, 20)
        assert upper == 1.0
        assert 0 < lower < 1.0

    def test_zero_proportion_lower_bound_is_zero(self):
        lower, upper = wilson_ci(0, 20)
        assert lower == 0.0
        assert 0 < upper < 1.0

    def test_widens_for_smaller_n(self):
        l_small, u_small = wilson_ci(5, 10)
        l_big, u_big = wilson_ci(500, 1000)
        # 50% in both cases, but small n should produce a wider interval.
        assert (u_small - l_small) > (u_big - l_big)
