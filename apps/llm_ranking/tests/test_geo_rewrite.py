"""
GEO content-rewrite service.

Tests inject a stub rewriter so we never hit Claude. The service is
pure orchestration: pick a method, enforce per-user daily quota, run
the LLM, summarize the diff.
"""
import pytest

from apps.llm_ranking.services import geo_rewrite as gr


def _rewriter(stub_text):
    """Build a stub rewriter that returns canned text and counts calls."""
    state = {"calls": 0, "last_system": "", "last_prompt": ""}

    def fn(*, system, user_prompt, user=None, audit_id=None):
        state["calls"] += 1
        state["last_system"] = system
        state["last_prompt"] = user_prompt
        return stub_text

    fn.state = state
    return fn


# ── Method registry ───────────────────────────────────────────────────────

def test_keyword_stuffing_and_unique_words_are_not_allowed():
    """The paper shows both regress visibility — they must not be reachable."""
    assert "keyword_stuffing" not in gr.ALLOWED_METHODS
    assert "unique_words" not in gr.ALLOWED_METHODS


def test_five_winning_methods_are_allowed():
    for m in (
        "quotation_addition",
        "statistics_addition",
        "fluency_optimization",
        "cite_sources",
        "authoritative",
    ):
        assert m in gr.ALLOWED_METHODS
        spec = gr.METHODS[m]
        assert spec["system"]
        assert spec["label"]
        assert spec["lift"] > 0


# ── Validation ────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_unknown_method_rejected(settings):
    settings.ANTHROPIC_API_KEY = "k"
    out = gr.rewrite(
        source_text="hello world",
        method="bogus",
        rewriter=_rewriter("ignored"),
    )
    assert out["ok"] is False
    assert out["error"] == "unknown_method"


@pytest.mark.django_db
def test_empty_source_rejected(settings):
    settings.ANTHROPIC_API_KEY = "k"
    out = gr.rewrite(
        source_text="   ",
        method="quotation_addition",
        rewriter=_rewriter("ignored"),
    )
    assert out["ok"] is False
    assert out["error"] == "empty_source"


@pytest.mark.django_db
def test_missing_api_key_when_using_default_rewriter(settings):
    settings.ANTHROPIC_API_KEY = ""
    out = gr.rewrite(source_text="hello", method="quotation_addition")
    assert out["ok"] is False
    assert out["error"] == "anthropic_key_missing"


# ── Happy path ────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_successful_rewrite_returns_diff_and_lift(settings):
    settings.ANTHROPIC_API_KEY = "k"
    settings.CLAUDE_REWRITE_DAILY_LIMIT_PER_USER = 10

    before = "Robots will replace humans. They are everywhere."
    after  = (
        "Robots are reshaping the workforce, according to MIT's 2024 report. "
        "A staggering 70% rise in deployments underscores the shift."
    )
    rewriter = _rewriter(after)

    out = gr.rewrite(
        source_text=before,
        method="statistics_addition",
        query="will robots replace humans",
        user_id=42,
        rewriter=rewriter,
    )

    assert out["ok"] is True
    assert out["method"] == "statistics_addition"
    assert out["label"] == "Add a statistic"
    assert out["projected_lift_pct"] == 32.1
    assert out["rewritten"] == after
    assert out["diff"]["before_words"] == 7
    assert out["diff"]["after_words"] > out["diff"]["before_words"]
    assert out["diff"]["delta_words"] > 0
    # The right method's system prompt was used.
    assert "statistic" in rewriter.state["last_system"].lower()
    # The query was forwarded so the rewriter has context.
    assert "will robots replace humans" in rewriter.state["last_prompt"]


# ── Quota ─────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_quota_blocks_when_daily_limit_reached(settings):
    settings.ANTHROPIC_API_KEY = "k"
    settings.CLAUDE_REWRITE_DAILY_LIMIT_PER_USER = 1

    rewriter = _rewriter("after")
    first = gr.rewrite(
        source_text="before text",
        method="quotation_addition",
        user_id=7,
        rewriter=rewriter,
    )
    assert first["ok"] is True

    second = gr.rewrite(
        source_text="before text",
        method="quotation_addition",
        user_id=7,
        rewriter=rewriter,
    )
    assert second["ok"] is False
    assert second["error"] == "quota_exceeded"
    assert second["daily_limit"] == 1
    # Rewriter was never called the second time.
    assert rewriter.state["calls"] == 1


@pytest.mark.django_db
def test_quota_remaining_counts_down(settings):
    settings.ANTHROPIC_API_KEY = "k"
    settings.CLAUDE_REWRITE_DAILY_LIMIT_PER_USER = 5

    rewriter = _rewriter("after")
    gr.rewrite(
        source_text="hello", method="cite_sources",
        user_id=8, rewriter=rewriter,
    )
    gr.rewrite(
        source_text="hello", method="cite_sources",
        user_id=8, rewriter=rewriter,
    )
    assert gr.quota_remaining(8) == 3


# ── Resilience ────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_rewriter_failure_does_not_raise(settings):
    settings.ANTHROPIC_API_KEY = "k"
    settings.CLAUDE_REWRITE_DAILY_LIMIT_PER_USER = 10

    def boom(**_):
        raise RuntimeError("anthropic 500")

    out = gr.rewrite(
        source_text="hello", method="fluency_optimization",
        user_id=9, rewriter=boom,
    )
    assert out["ok"] is False
    assert out["error"] == "rewriter_error"
