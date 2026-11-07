"""Tests for the Plackett-Luce ranking model."""
from apps.llm_ranking.services.plackett_luce import (
    fit_plackett_luce,
    rankings_from_results,
)


class _Result:
    """Lightweight stand-in for LLMRankingResult — avoids DB I/O in unit tests."""

    def __init__(self, *, query_succeeded=True, is_mentioned=False,
                 mention_rank=None, competitors_mentioned=None):
        self.query_succeeded = query_succeeded
        self.is_mentioned = is_mentioned
        self.mention_rank = mention_rank
        self.competitors_mentioned = competitors_mentioned or []


class TestFitPlackettLuce:
    def test_recovers_known_order(self):
        # A always beats B, B usually beats C
        rankings = [
            ["A", "B", "C"],
            ["A", "B", "C"],
            ["A", "C", "B"],
            ["A", "B", "C"],
            ["B", "A", "C"],
        ]
        s = fit_plackett_luce(rankings)
        assert s["A"] > s["B"] > s["C"]

    def test_empty_input(self):
        assert fit_plackett_luce([]) == {}

    def test_single_brand_rankings_skipped(self):
        # Rankings of length < 2 carry no information and shouldn't crash.
        assert fit_plackett_luce([["A"]]) == {}

    def test_normalises_to_one(self):
        rankings = [["A", "B"], ["A", "B"], ["B", "A"]]
        s = fit_plackett_luce(rankings)
        assert max(s.values()) == 1.0
        assert all(0 < v <= 1.0 for v in s.values())

    def test_dominant_brand_close_to_one(self):
        rankings = [["X", "Y", "Z"]] * 20
        s = fit_plackett_luce(rankings)
        assert s["X"] == 1.0
        assert s["Y"] > s["Z"]


class TestRankingsFromResults:
    def test_builds_ranking_from_target_and_competitors(self):
        results = [_Result(
            query_succeeded=True, is_mentioned=True, mention_rank=2,
            competitors_mentioned=[
                {"name": "Mixpanel", "position": 1},
                {"name": "Heap", "position": 3},
            ],
        )]
        rankings = rankings_from_results(results, target_name="Cansee")
        assert rankings == [["Mixpanel", "Cansee", "Heap"]]

    def test_skips_failed_queries(self):
        results = [_Result(query_succeeded=False)]
        assert rankings_from_results(results, target_name="Cansee") == []

    def test_skips_rankings_with_one_entry(self):
        # Only target mentioned with no competitors → no ranking signal.
        results = [_Result(
            query_succeeded=True, is_mentioned=True, mention_rank=1,
            competitors_mentioned=[],
        )]
        assert rankings_from_results(results, target_name="Cansee") == []

    def test_handles_missing_positions(self):
        results = [_Result(
            query_succeeded=True, is_mentioned=False,
            competitors_mentioned=[
                {"name": "Mixpanel"},  # no position
                {"name": "Heap", "position": 2},
                {"name": "Amplitude", "position": 1},
            ],
        )]
        rankings = rankings_from_results(results, target_name="Cansee")
        assert rankings == [["Amplitude", "Heap"]]
