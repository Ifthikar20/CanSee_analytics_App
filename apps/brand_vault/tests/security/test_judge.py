"""Judge parsing tests. The upstream Claude call is bypassed by _parse_verdict."""
from __future__ import annotations

from apps.brand_vault.services.security.judge import _parse_verdict

ALLOWED = ("hallucination", "negative")


def test_parses_plain_json():
    raw = (
        '{"issue":"hallucination","severity":"high",'
        ' "sentiment_score":-0.4,"detail":"wrong price"}'
    )
    v = _parse_verdict(raw, ALLOWED)
    assert v.issue == "hallucination"
    assert v.severity == "high"
    assert v.sentiment_score == -0.4
    assert v.detail == "wrong price"


def test_parses_fenced_json():
    raw = "Here is the verdict:\n```json\n{\"issue\":\"negative\",\"severity\":\"low\"}\n```"
    v = _parse_verdict(raw, ALLOWED)
    assert v.issue == "negative"
    assert v.severity == "low"


def test_rejects_issue_outside_allowed_set():
    raw = '{"issue":"impersonation","severity":"high"}'
    v = _parse_verdict(raw, ALLOWED)
    assert v.issue == "none"


def test_defaults_when_bad_input():
    v = _parse_verdict("not json at all", ALLOWED)
    assert v.issue == "none"
    assert v.severity == "none"
    assert v.sentiment_score is None
