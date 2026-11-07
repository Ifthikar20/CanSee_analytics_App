"""
G-Eval subjective-impression scoring service.

Tests use an injected ``judge`` callable so they never hit Claude —
the service is pure orchestration around an LLM call plus quota and
JSON parsing.
"""
import json

import pytest

from apps.llm_ranking.services import subjective_impression as si


def _judge_factory(scores_per_metric, *, fail_first=False):
    """
    Build a stub judge that returns a JSON envelope with the given
    1-5 scores. ``fail_first`` makes the first call raise so we can
    test the "average over surviving samples" path.
    """
    state = {"calls": 0}

    def judge(prompt, *, user=None, audit_id=None):
        state["calls"] += 1
        if fail_first and state["calls"] == 1:
            raise RuntimeError("fake parse error")
        body = {
            m: {"score": scores_per_metric[m], "reason": f"{m} reason"}
            for m in si.SUB_METRICS
        }
        return json.dumps(body)

    judge.state = state
    return judge


@pytest.mark.django_db
def test_returns_empty_when_query_or_response_missing(settings):
    out = si.score_citation(
        query="", response_text="hi", citation_index=1, citation_url="https://x",
        judge=lambda *a, **k: "{}",
    )
    assert out["average"] is None
    assert out["relevance"]["score"] is None


@pytest.mark.django_db
def test_returns_empty_when_no_anthropic_key(settings):
    settings.ANTHROPIC_API_KEY = ""
    out = si.score_citation(
        query="q", response_text="r [1]",
        citation_index=1, citation_url="https://x",
    )
    # No judge override → default → key missing → empty shell.
    assert out["average"] is None


@pytest.mark.django_db
def test_single_sample_returns_scores(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 10

    judge = _judge_factory({m: 4 for m in si.SUB_METRICS})
    out = si.score_citation(
        query="q", response_text="A response [1].",
        citation_index=1, citation_url="https://x",
        samples=1, user_id=1, judge=judge,
    )
    assert out["average"] == 4.0
    for m in si.SUB_METRICS:
        assert out[m]["score"] == 4
        assert out[m]["samples"] == 1
    assert out["samples"] == 1
    assert judge.state["calls"] == 1


@pytest.mark.django_db
def test_multi_sample_averages_scores(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 10

    # Vary across samples — different scores per call.
    calls = [{m: 3 for m in si.SUB_METRICS}, {m: 5 for m in si.SUB_METRICS}]

    def judge(prompt, *, user=None, audit_id=None):
        return json.dumps({
            m: {"score": calls.pop(0).get(m, 4), "reason": "r"}
            if calls else {"score": 5, "reason": "r"}
            for m in si.SUB_METRICS
        })

    # Simpler: build a stateful judge that uses a counter.
    state = {"i": 0}
    samples_data = [
        {m: 3 for m in si.SUB_METRICS},
        {m: 5 for m in si.SUB_METRICS},
    ]

    def judge2(prompt, *, user=None, audit_id=None):
        i = state["i"]
        state["i"] += 1
        body = samples_data[i]
        return json.dumps({m: {"score": body[m], "reason": "r"} for m in si.SUB_METRICS})

    out = si.score_citation(
        query="q", response_text="A response [1].",
        citation_index=1, citation_url="https://x",
        samples=2, user_id=2, judge=judge2,
    )
    assert out["samples"] == 2
    for m in si.SUB_METRICS:
        assert out[m]["score"] == 4.0  # mean of 3, 5
        assert out[m]["samples"] == 2
    assert out["average"] == 4.0


@pytest.mark.django_db
def test_failed_sample_doesnt_kill_the_run(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 10

    judge = _judge_factory(
        {m: 4 for m in si.SUB_METRICS}, fail_first=True,
    )
    out = si.score_citation(
        query="q", response_text="A response [1].",
        citation_index=1, citation_url="https://x",
        samples=2, user_id=3, judge=judge,
    )
    # 1 sample succeeded → score is the surviving value.
    assert out["samples"] == 1
    assert out["average"] == 4.0


@pytest.mark.django_db
def test_quota_blocks_further_judge_calls(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 1  # only 1 call allowed

    judge = _judge_factory({m: 5 for m in si.SUB_METRICS})

    # First citation consumes the budget.
    out1 = si.score_citation(
        query="q", response_text="r [1]",
        citation_index=1, citation_url="https://x",
        samples=1, user_id=99, judge=judge,
    )
    assert out1["average"] == 5.0

    # Second citation is refused — empty shell.
    out2 = si.score_citation(
        query="q", response_text="r [2]",
        citation_index=2, citation_url="https://y",
        samples=1, user_id=99, judge=judge,
    )
    assert out2["average"] is None
    assert out2["samples"] == 0
    # Judge was only called once total — second call was quota-blocked.
    assert judge.state["calls"] == 1


@pytest.mark.django_db
def test_clamps_out_of_range_scores(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 10

    def judge(prompt, *, user=None, audit_id=None):
        return json.dumps({
            m: {"score": 99 if m == "relevance" else 0, "reason": "r"}
            for m in si.SUB_METRICS
        })

    out = si.score_citation(
        query="q", response_text="r [1]",
        citation_index=1, citation_url="https://x",
        samples=1, user_id=4, judge=judge,
    )
    # 99 -> 5 (upper clamp), 0 -> 1 (lower clamp)
    assert out["relevance"]["score"] == 5
    assert out["influence"]["score"] == 1


@pytest.mark.django_db
def test_unparseable_judge_response_returns_empty(settings):
    settings.ANTHROPIC_API_KEY = "key"
    settings.CLAUDE_JUDGE_DAILY_LIMIT_PER_USER = 10

    def judge(prompt, *, user=None, audit_id=None):
        return "no json here just prose"

    out = si.score_citation(
        query="q", response_text="r [1]",
        citation_index=1, citation_url="https://x",
        samples=1, user_id=5, judge=judge,
    )
    assert out["average"] is None
