"""Tests for the context-driven prompt generator."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from apps.prompt_library.services.context_generator import (
    generate_from_context,
    generate_related_prompts,
)


class _Fake:
    succeeded = True

    def __init__(self, text: str):
        self.text = text


def _provider(text: str, name: str = "deepseek"):
    p = SimpleNamespace(name=name)
    p.query = lambda *a, **kw: _Fake(text)
    return p


def test_no_provider_returns_fallback():
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=None,
    ):
        items, provider = generate_from_context("a real context with words")
    assert provider == "fallback"
    assert len(items) == 1
    assert "{{" in items[0]["template_text"]


def test_parses_strict_json_array():
    payload = (
        "["
        '{"template_text": "I keep hearing about {{ company_name }} near {{ location }}.",'
        ' "style": "story", "intent_bucket": "local",'
        ' "preview_text": "I keep hearing about Acme near Broadway."},'
        '{"template_text": "Has the FTC gone after {{ company_name }}?",'
        ' "style": "question", "intent_bucket": "problem",'
        ' "preview_text": "Has the FTC gone after Acme?"}'
        "]"
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, provider = generate_from_context("podcast about health Broadway NY 6pm")
    assert provider == "deepseek"
    assert len(items) == 2
    assert items[0]["style"] == "story"
    # Variables re-extracted from template_text.
    assert "company_name" in items[0]["template_variables"]
    assert "location" in items[0]["template_variables"]
    assert items[1]["intent_bucket"] == "problem"


def test_strips_markdown_fences():
    payload = (
        "```json\n"
        '[{"template_text": "Have you tried {{ brand }} in {{ location }}?",'
        ' "style": "question", "intent_bucket": "category",'
        ' "preview_text": "Have you tried Acme in NYC?"}]'
        "\n```"
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, _ = generate_from_context("a context with five words here")
    assert len(items) == 1
    assert items[0]["template_variables"] == ["brand", "location"]


def test_invalid_style_normalized():
    payload = (
        '[{"template_text": "Anyone heard of {{ company_name }}?",'
        ' "style": "rant", "intent_bucket": "weird",'
        ' "preview_text": "Anyone heard of Acme?"}]'
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, _ = generate_from_context("podcast about health Broadway NY 6pm")
    assert items[0]["style"] == "question"
    assert items[0]["intent_bucket"] == "category"


def test_short_template_text_skipped_then_fallback():
    payload = '[{"template_text": "hi", "style": "question", "intent_bucket": "category"}]'
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, provider = generate_from_context("podcast about health Broadway NY 6pm")
    # Item dropped, falls back to single safe template.
    assert provider == "deepseek"
    assert len(items) == 1
    assert "{{" in items[0]["template_text"]


def test_provider_exception_falls_back():
    boom = SimpleNamespace(name="deepseek")

    def _raise(*a, **kw):
        raise RuntimeError("network")

    boom.query = _raise
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=boom,
    ):
        items, provider = generate_from_context("a real context with words")
    assert provider == "deepseek"
    assert len(items) == 1


def test_related_blank_seed_returns_empty():
    # No seed means nothing to relate to; return an empty list (not a
    # fallback row) so the caller can render nothing.
    items, provider = generate_related_prompts("   ")
    assert items == []
    assert provider == "fallback"


def test_related_short_seed_still_generates():
    # Unlike the free-form context path, a short seed prompt is allowed —
    # the service wraps it into a small instruction before the AI call.
    payload = (
        '[{"prompt_text": "Which CRM is best for a two-person agency?",'
        ' "style": "comparison", "intent_bucket": "comparison"}]'
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, provider = generate_related_prompts("best CRM?", count=3)
    assert provider == "deepseek"
    assert len(items) == 1
    assert items[0]["style"] == "comparison"


def test_related_pins_deepseek_and_meters_per_user():
    # The seed must reach DeepSeek (cheapest) and the call must carry the
    # user + a distinct role so token usage is metered per-user.
    captured = {}

    def _query(prompt, system, **kw):
        captured["prompt"] = prompt
        captured.update(kw)
        return _Fake(
            '[{"prompt_text": "Which CRM fits a two-person team?",'
            ' "style": "comparison", "intent_bucket": "comparison"}]'
        )

    provider = SimpleNamespace(name="deepseek")
    provider.query = _query
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=provider,
    ) as factory:
        items, name = generate_related_prompts("best CRM?", count=4, user="user-123")

    factory.assert_called_once_with("deepseek")  # pinned, not the default chain
    assert name == "deepseek"
    assert captured["user"] == "user-123"        # metered against the user
    assert captured["role"] == "recommendation"  # distinct usage tag
    assert "best CRM?" in captured["prompt"]
    assert len(items) == 1


def test_related_returns_empty_on_provider_failure():
    boom = SimpleNamespace(name="deepseek")

    def _raise(*a, **kw):
        raise RuntimeError("network")

    boom.query = _raise
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=boom,
    ):
        items, name = generate_related_prompts("best CRM?")
    # Best-effort: no junk fallback row, just nothing to show.
    assert items == []
    assert name == "deepseek"


def test_truncated_json_is_repaired():
    payload = (
        '[{"template_text": "Anyone tried {{ company_name }} near {{ location }}?",'
        ' "style": "question", "intent_bucket": "local",'
        ' "preview_text": "Anyone tried Acme near Broadway?"},'
        '{"template_text": "Tell me about {{ company_name }} in'
    )
    with patch(
        "apps.llm_ranking.providers.get_synthesis_provider",
        return_value=_provider(payload),
    ):
        items, _ = generate_from_context("podcast about health Broadway NY 6pm")
    assert len(items) >= 1
    assert "company_name" in items[0]["template_variables"]
