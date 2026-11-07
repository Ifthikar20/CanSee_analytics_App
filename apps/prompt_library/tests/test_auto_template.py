"""Tests for the synthesis-provider-backed auto_template service."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from apps.prompt_library.services.auto_template import auto_template


class _Fake:
    succeeded = True

    def __init__(self, text: str):
        self.text = text


def _provider(text: str):
    p = SimpleNamespace()
    p.query = lambda *a, **kw: _Fake(text)
    return p


def test_fallback_when_no_provider():
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=None,
    ):
        out = auto_template("Best CRM in Dallas", user=None)
    assert out["template_text"] == "Best CRM in Dallas"
    assert out["template_variables"] == []
    assert out["style"] == "question"


def test_extracts_envelope_from_provider_response():
    payload = (
        '{"template_text": "Best CRM in {{ location }}?", '
        '"template_variables": ["location"], "style": "local"}'
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        out = auto_template("Best CRM in Dallas", user=None)
    assert "{{ location }}" in out["template_text"]
    assert "location" in out["template_variables"]
    assert out["style"] == "local"


def test_invalid_style_falls_back_to_question():
    payload = (
        '{"template_text": "Hi {{ name }}", '
        '"template_variables": ["name"], "style": "rant"}'
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        out = auto_template("Hi Sam", user=None)
    assert out["style"] == "question"


def test_malformed_json_falls_back():
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider("not json at all"),
    ):
        out = auto_template("raw", user=None)
    assert out["template_text"] == "raw"
    assert out["template_variables"] == []


def test_empty_input_short_circuits():
    out = auto_template("", user=None)
    assert out["template_text"] == ""
    assert out["template_variables"] == []
