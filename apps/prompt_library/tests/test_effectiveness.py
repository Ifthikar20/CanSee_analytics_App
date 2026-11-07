"""Tests for the prompt effectiveness scorer."""
from __future__ import annotations

from unittest.mock import patch

from apps.prompt_library.services.effectiveness import (
    MIN_RUNS_STABLE,
    compute_effectiveness,
)


class _FakeQS:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, **kwargs):
        rows = self._rows
        if "is_mentioned" in kwargs:
            rows = [r for r in rows if r.is_mentioned == kwargs["is_mentioned"]]
        if "source_prompt" in kwargs:
            rows = [r for r in rows if r.source_prompt == kwargs["source_prompt"]]
        return _FakeQS(rows)

    def values(self, *fields):
        rows = [{f: getattr(r, f) for f in fields} for r in self._rows]
        seen = set()
        unique = []
        for d in rows:
            key = tuple(d.items())
            if key not in seen:
                seen.add(key)
                unique.append(d)
        return _FakeQS(unique)

    def distinct(self):
        return self

    def count(self):
        return len(self._rows)

    def only(self, *fields):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeRow:
    def __init__(self, *, is_mentioned, citations, provider, source_prompt):
        self.is_mentioned = is_mentioned
        self.citations = citations
        self.provider = provider
        self.source_prompt = source_prompt


class _FakePrompt:
    pk = "p"


def _patch_qs(rows):
    fake = _FakeQS(rows)

    class _Manager:
        @staticmethod
        def filter(**kwargs):
            return fake.filter(**kwargs)

    return patch(
        "apps.llm_ranking.models.LLMRankingResult.objects",
        new=_Manager,
    )


def test_no_runs_is_unstable():
    with _patch_qs([]):
        env = compute_effectiveness(_FakePrompt())
    assert env["stable"] is False
    assert env["overall"] == 0.0
    assert env["runs"] == 0


def test_below_threshold_is_unstable():
    prompt = _FakePrompt()
    rows = [
        _FakeRow(is_mentioned=True, citations=[], provider="claude", source_prompt=prompt),
    ]
    with _patch_qs(rows):
        env = compute_effectiveness(prompt)
    assert env["stable"] is False
    assert env["runs"] == 1


def test_full_score_perfect_signals():
    prompt = _FakePrompt()
    rows = [
        _FakeRow(
            is_mentioned=True,
            citations=["a", "b", "c"],
            provider=p,
            source_prompt=prompt,
        )
        for p in ("claude", "gpt4", "gemini", "perplexity")
    ]
    with _patch_qs(rows):
        env = compute_effectiveness(prompt)
    assert env["runs"] == 4
    assert env["stable"] is True
    assert env["components"]["visibility_hit_rate"] == 1.0
    assert env["components"]["citation_yield"] == 1.0
    assert env["components"]["coverage_breadth"] == 1.0
    # Claim_yield is 0 when claim_verifier isn't queryable, so overall
    # caps at 0.4 + 0.3 + 0.1 = 0.80.
    assert env["overall"] >= 0.79


def test_components_clamped():
    prompt = _FakePrompt()
    rows = [
        _FakeRow(
            is_mentioned=False,
            citations=["a"] * 100,  # huge — should clamp to 1.0
            provider="claude",
            source_prompt=prompt,
        )
    ] * MIN_RUNS_STABLE
    with _patch_qs(rows):
        env = compute_effectiveness(prompt)
    assert env["components"]["citation_yield"] == 1.0
    assert env["components"]["visibility_hit_rate"] == 0.0
