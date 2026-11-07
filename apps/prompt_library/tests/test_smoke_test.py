"""Tests for the prompt smoke-test service."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.prompt_library.services.smoke_test import smoke_test_prompt


class _OK:
    succeeded = True
    duration_ms = 42

    def __init__(self, text: str):
        self.text = text


def _website():
    return SimpleNamespace(name="Acme", industry="x", locations=[], prompt_variables=None)


def _prompt():
    p = MagicMock()
    p.template_text = "What does {{ company_name }} do?"
    p.text = ""
    return p


def test_returns_error_when_provider_missing():
    with patch("apps.llm_ranking.providers.get_provider", return_value=None):
        out = smoke_test_prompt(_prompt(), _website(), provider="claude")
    assert out["ok"] is False
    assert "not_configured" in out["error"]


def test_succeeds_and_detects_mention():
    fake = SimpleNamespace(
        query=lambda *a, **kw: _OK("Acme is a great company. https://acme.com"),
    )
    with patch("apps.llm_ranking.providers.get_provider", return_value=fake):
        out = smoke_test_prompt(_prompt(), _website(), provider="claude")
    assert out["ok"] is True
    assert out["mentioned"] is True
    assert out["citations_count"] >= 1
    assert out["filled_prompt"] == "What does Acme do?"


def test_handles_provider_exception():
    def _boom(*a, **kw):
        raise RuntimeError("upstream timeout")

    fake = SimpleNamespace(query=_boom)
    with patch("apps.llm_ranking.providers.get_provider", return_value=fake):
        out = smoke_test_prompt(_prompt(), _website(), provider="claude")
    assert out["ok"] is False
    assert "upstream timeout" in out["error"]
