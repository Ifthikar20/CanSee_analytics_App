"""OpenAI + grok providers: Responses-API web search citations.

Both providers ask the OpenAI-compatible Responses API for web search and
must surface the url_citation annotations as ProviderResult.citations —
plus fall back to a plain chat completion when the Responses call fails.
The parser tolerates dict- and object-shaped items (SDK drift trap).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.llm_ranking.providers.grok import GrokProvider
from apps.llm_ranking.providers.openai import OpenAIProvider
from apps.llm_ranking.providers.openai_compat import extract_responses_citations


def _responses_payload(urls, as_dicts=True, top_level=()):
    def ann(u):
        return ({"type": "url_citation", "url": u, "title": "t"} if as_dicts
                else SimpleNamespace(type="url_citation", url=u, title="t"))
    message = {
        "type": "message",
        "content": [{
            "type": "output_text",
            "text": "answer",
            "annotations": [ann(u) for u in urls],
        }],
    }
    ws_call = {"type": "web_search_call", "status": "completed"}
    resp = SimpleNamespace(
        output=[ws_call, message],
        output_text="1. **Acme** - cited answer.",
        usage=SimpleNamespace(input_tokens=5, output_tokens=9),
    )
    if top_level:
        resp.citations = list(top_level)
    return resp


class TestExtractResponsesCitations:
    def test_annotation_urls_in_order_deduped(self):
        resp = _responses_payload([
            "https://a.com/x", "https://b.com/y", "https://a.com/x",
        ])
        assert extract_responses_citations(resp) == [
            "https://a.com/x", "https://b.com/y",
        ]

    def test_object_shaped_annotations_also_parse(self):
        resp = _responses_payload(["https://a.com/x"], as_dicts=False)
        assert extract_responses_citations(resp) == ["https://a.com/x"]

    def test_top_level_citations_list_appended(self):
        # xAI's legacy shape: bare URL strings on the response object.
        resp = _responses_payload(
            ["https://a.com/x"], top_level=["https://c.com/z", "https://a.com/x"],
        )
        assert extract_responses_citations(resp) == [
            "https://a.com/x", "https://c.com/z",
        ]

    def test_capped_at_20(self):
        urls = [f"https://s{i}.com/" for i in range(30)]
        assert len(extract_responses_citations(_responses_payload(urls))) == 20

    def test_empty_output_yields_empty(self):
        assert extract_responses_citations(SimpleNamespace(output=[])) == []


class TestOpenAIWebSearch:
    def _client(self, resp=None, raise_on=()):
        client = MagicMock()
        calls = []

        def create(**kwargs):
            tool_type = kwargs["tools"][0]["type"]
            calls.append(tool_type)
            if tool_type in raise_on:
                raise RuntimeError("tool not available")
            return resp
        client.responses.create.side_effect = create
        client._tool_calls = calls
        return client

    def test_websearch_result_carries_citations(self, settings):
        settings.OPENAI_API_KEY = "test-key"
        settings.LLM_WEBSEARCH_ENABLED = True
        client = self._client(_responses_payload(["https://a.com/x"]))
        with patch("openai.OpenAI", return_value=client):
            result = OpenAIProvider()._call(prompt="best?", system_prompt="sys")
        assert result.succeeded is True
        assert result.citations == ["https://a.com/x"]
        assert client._tool_calls == ["web_search"]
        # The search is forced — optional browsing leaves mini answering
        # from training data with an empty Sources panel.
        sent = client.responses.create.call_args.kwargs
        assert sent["tool_choice"] == {"type": "web_search"}

    def test_falls_back_to_preview_tool_name(self, settings):
        settings.OPENAI_API_KEY = "test-key"
        settings.LLM_WEBSEARCH_ENABLED = True
        client = self._client(
            _responses_payload(["https://a.com/x"]), raise_on=("web_search",),
        )
        with patch("openai.OpenAI", return_value=client):
            result = OpenAIProvider()._call(prompt="best?", system_prompt="sys")
        assert result.citations == ["https://a.com/x"]
        assert client._tool_calls == ["web_search", "web_search_preview"]

    def test_both_tool_names_failing_falls_back_to_chat(self, settings):
        settings.OPENAI_API_KEY = "test-key"
        settings.LLM_WEBSEARCH_ENABLED = True
        client = self._client(raise_on=("web_search", "web_search_preview"))
        chat_resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plain"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )
        client.chat.completions.create.return_value = chat_resp
        with patch("openai.OpenAI", return_value=client):
            result = OpenAIProvider()._call(prompt="best?", system_prompt="sys")
        assert result.text == "plain"
        assert result.citations == []


class TestGrokWebSearch:
    def test_websearch_result_carries_citations(self, settings):
        settings.XAI_API_KEY = "test-key"
        settings.LLM_WEBSEARCH_ENABLED = True
        client = MagicMock()
        client.responses.create.return_value = _responses_payload(
            ["https://a.com/x"], top_level=["https://c.com/z"],
        )
        with patch("openai.OpenAI", return_value=client):
            result = GrokProvider()._call(prompt="best?", system_prompt="sys")
        assert result.citations == ["https://a.com/x", "https://c.com/z"]
        sent = client.responses.create.call_args.kwargs
        assert sent["tools"] == [{"type": "web_search"}]

    def test_responses_failure_falls_back_to_chat(self, settings):
        settings.XAI_API_KEY = "test-key"
        settings.LLM_WEBSEARCH_ENABLED = True
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("no responses api")
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plain"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )
        with patch("openai.OpenAI", return_value=client):
            result = GrokProvider()._call(prompt="best?", system_prompt="sys")
        assert result.text == "plain"
        assert result.citations == []


class TestExtractionStrategy:
    def test_gpt4_and_grok_use_native_extractor_first(self):
        from apps.citations.services.extraction_service import _strategy_for_provider
        from apps.citations.services.extractors.perplexity import (
            PerplexityNativeExtractor,
        )

        for provider in ("gpt4", "grok"):
            strategy = _strategy_for_provider(provider)
            assert isinstance(strategy[0], PerplexityNativeExtractor)
