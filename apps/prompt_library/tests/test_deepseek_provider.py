"""Tests for the DeepSeek provider adapter."""
from __future__ import annotations

from unittest.mock import patch

from django.test import override_settings

from apps.llm_ranking.providers import (
    PROVIDERS,
    TOOLING_PROVIDERS,
    DeepSeekProvider,
    get_provider,
    get_synthesis_provider,
)


def test_deepseek_not_in_audit_registry():
    """DeepSeek must never be selectable by audit-side code paths."""
    assert "deepseek" not in PROVIDERS
    assert get_provider("deepseek") is None


def test_deepseek_in_tooling_registry():
    assert "deepseek" in TOOLING_PROVIDERS
    assert TOOLING_PROVIDERS["deepseek"] is DeepSeekProvider


@override_settings(DEEPSEEK_API_KEY="", PROMPT_SYNTHESIS_PROVIDER="deepseek")
def test_synthesis_falls_back_when_deepseek_unset():
    # No DeepSeek key — should fall back to whatever else is configured.
    # If nothing's configured, returns None gracefully.
    provider = get_synthesis_provider()
    if provider is not None:
        assert provider.name != "deepseek"


@override_settings(DEEPSEEK_API_KEY="sk-test", PROMPT_SYNTHESIS_PROVIDER="deepseek")
def test_synthesis_picks_deepseek_when_configured():
    provider = get_synthesis_provider()
    assert provider is not None
    assert provider.name == "deepseek"


def test_provider_label_maps_to_deepseek():
    p = DeepSeekProvider()
    assert p._provider_label() == "deepseek"


@override_settings(DEEPSEEK_API_KEY="sk-test")
def test_call_shape_matches_other_providers():
    """DeepSeekProvider._call posts to /chat/completions with bearer auth."""
    p = DeepSeekProvider()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            }

    with patch("apps.llm_ranking.providers.deepseek.requests.post", return_value=_Resp()) as mock:
        result = p._call(prompt="hello", system_prompt="sys")
    assert result.succeeded is True
    assert result.text == "hi"
    assert result.input_tokens == 5
    assert result.output_tokens == 7
    args, kwargs = mock.call_args
    assert args[0].endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
