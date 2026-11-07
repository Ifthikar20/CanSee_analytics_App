"""Claude provider: web-search citations become ProviderResult.citations.

The Anthropic web search server tool reports its sources in-band: text
blocks carry a ``citations`` list for the passages they ground, and
``web_search_tool_result`` blocks list every page the search retrieved.
The provider must surface those URLs (cited first, retrieved second,
deduped, capped) so the chat modal can show real sources for Claude the
same way it does for Perplexity. A failed search returns an error OBJECT
as the tool_result content (HTTP 200, no exception) and must be skipped.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.llm_ranking.providers.claude import ClaudeProvider


def _text(text, citations=None, as_dicts=True):
    """Text block. On the pinned SDK, citation entries are plain dicts
    (the field is unknown to it and lands unparsed in model_extra);
    ``as_dicts=False`` models a newer SDK that types them."""
    block = SimpleNamespace(type="text", text=text)
    if citations is not None:
        block.citations = [
            {"type": "web_search_result_location", "url": u} if as_dicts
            else SimpleNamespace(url=u)
            for u in citations
        ]
    return block


def _search_result(urls, as_dicts=True):
    return SimpleNamespace(
        type="web_search_tool_result",
        content=[
            {"type": "web_search_result", "url": u, "title": "t"} if as_dicts
            else SimpleNamespace(url=u)
            for u in urls
        ],
    )


def _resp(blocks):
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )


def _run(blocks, settings, websearch=True):
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.LLM_WEBSEARCH_ENABLED = websearch
    client = MagicMock()
    client.messages.create.return_value = _resp(blocks)
    with patch("anthropic.Anthropic", return_value=client):
        return ClaudeProvider()._call(prompt="best?", system_prompt="sys")


class TestClaudeCitations:
    def test_cited_urls_come_first_then_retrieved(self, settings):
        result = _run([
            _search_result(["https://a.com/1", "https://b.com/2", "https://c.com/3"]),
            _text("Intro. "),
            _text("Acme is best", citations=["https://b.com/2"]),
            _text(" overall."),
        ], settings)
        assert result.citations == [
            "https://b.com/2", "https://a.com/1", "https://c.com/3",
        ]

    def test_typed_sdk_objects_also_parse(self, settings):
        # Newer SDKs type the citation/search entries instead of dicts.
        result = _run([
            _search_result(["https://a.com/1"], as_dicts=False),
            _text("Acme", citations=["https://b.com/2"], as_dicts=False),
        ], settings)
        assert result.citations == ["https://b.com/2", "https://a.com/1"]

    def test_error_object_content_is_skipped(self, settings):
        # Server-tool errors arrive as an object, not a list of results.
        errored = SimpleNamespace(
            type="web_search_tool_result",
            content=SimpleNamespace(type="web_search_tool_result_error",
                                    error_code="max_uses_exceeded"),
        )
        result = _run([errored, _text("answer")], settings)
        assert result.succeeded is True
        assert result.citations == []

    def test_text_joined_without_injected_spaces(self, settings):
        # Cited answers arrive split mid-sentence; joining with " " used to
        # render "budget allows ." in the UI.
        result = _run([
            _text("Saves when your budget allows", citations=["https://a.com"]),
            _text(". Next sentence."),
        ], settings)
        assert result.text == "Saves when your budget allows. Next sentence."

    def test_dedupe_and_cap_at_20(self, settings):
        urls = [f"https://site{i}.com/page" for i in range(30)]
        result = _run([
            _text("x", citations=[urls[0], urls[0]]),
            _search_result(urls),
        ], settings)
        assert len(result.citations) == 20
        assert result.citations[0] == urls[0]
        assert len(set(result.citations)) == 20

    def test_no_websearch_response_has_no_citations(self, settings):
        result = _run([_text("plain answer")], settings, websearch=False)
        assert result.citations == []
        assert result.text == "plain answer"


class TestClaudeExtractionStrategy:
    def test_claude_uses_native_extractor_first(self):
        from apps.citations.services.extraction_service import _strategy_for_provider
        from apps.citations.services.extractors.perplexity import (
            PerplexityNativeExtractor,
        )

        strategy = _strategy_for_provider("claude")
        assert isinstance(strategy[0], PerplexityNativeExtractor)
