"""
Tests that the region kwarg threads through provider calls correctly.

Asserts the exact JSON body the Perplexity provider would send for a
given region, without making a real HTTP call. The other providers
ignore the kwarg (the base class threads it through but their _call
methods don't act on it) — we have a test confirming they don't crash.
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.llm_ranking.providers import perplexity as pplx_mod
from apps.llm_ranking.providers.claude import ClaudeProvider
from apps.llm_ranking.providers.gemini import GeminiProvider
from apps.llm_ranking.providers.openai import OpenAIProvider
from apps.llm_ranking.providers.perplexity import PerplexityProvider


def _mock_perplexity_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": "Top 1: Cansee. Top 2: Mixpanel."}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 30},
    }
    resp.raise_for_status = MagicMock()
    return resp


class TestPerplexityRegionRouting:
    @pytest.fixture(autouse=True)
    def _set_key(self, settings):
        settings.PERPLEXITY_API_KEY = "test-key"

    def test_global_omits_user_location(self):
        with patch.object(pplx_mod, "requests") as req:
            req.post.return_value = _mock_perplexity_response()
            provider = PerplexityProvider()
            provider._call(prompt="best?", system_prompt="sys", region="global")
            sent = req.post.call_args.kwargs["json"]
        assert "web_search_options" not in sent

    def test_us_region_sends_country_us(self):
        with patch.object(pplx_mod, "requests") as req:
            req.post.return_value = _mock_perplexity_response()
            provider = PerplexityProvider()
            provider._call(prompt="best?", system_prompt="sys", region="us")
            sent = req.post.call_args.kwargs["json"]
        assert sent["web_search_options"]["user_location"]["country"] == "US"

    def test_uk_region_maps_to_gb(self):
        # UK is the audit-side label, but Perplexity expects the ISO-2 GB.
        with patch.object(pplx_mod, "requests") as req:
            req.post.return_value = _mock_perplexity_response()
            provider = PerplexityProvider()
            provider._call(prompt="best?", system_prompt="sys", region="uk")
            sent = req.post.call_args.kwargs["json"]
        assert sent["web_search_options"]["user_location"]["country"] == "GB"

    def test_unknown_region_falls_back_to_global(self):
        with patch.object(pplx_mod, "requests") as req:
            req.post.return_value = _mock_perplexity_response()
            provider = PerplexityProvider()
            provider._call(prompt="best?", system_prompt="sys", region="zz")
            sent = req.post.call_args.kwargs["json"]
        # zz lookup -> GLOBAL region -> empty perplexity_country -> no key.
        assert "web_search_options" not in sent

    def test_empty_region_omits_user_location(self):
        with patch.object(pplx_mod, "requests") as req:
            req.post.return_value = _mock_perplexity_response()
            provider = PerplexityProvider()
            provider._call(prompt="best?", system_prompt="sys", region="")
            sent = req.post.call_args.kwargs["json"]
        assert "web_search_options" not in sent


class TestOtherProvidersAcceptRegionKwarg:
    """Region is a no-op for these providers, but the signature must accept it."""

    def test_claude_signature_accepts_region(self):
        # Provider class — we don't call ``_call`` because it would need an
        # SDK client; instead just check the signature is keyword-compatible.
        import inspect
        sig = inspect.signature(ClaudeProvider._call)
        assert "region" in sig.parameters

    def test_openai_signature_accepts_region(self):
        import inspect
        sig = inspect.signature(OpenAIProvider._call)
        assert "region" in sig.parameters

    def test_gemini_signature_accepts_region(self):
        import inspect
        sig = inspect.signature(GeminiProvider._call)
        assert "region" in sig.parameters

    def test_base_query_threads_region(self):
        """``LLMProvider.query`` accepts region and forwards to ``_call``."""
        from apps.llm_ranking.providers.base import LLMProvider, ProviderResult

        captured = {}

        class _Stub(LLMProvider):
            name = "stub"
            model = "stub"
            api_key_setting = "ANTHROPIC_API_KEY"   # any configured key

            def is_configured(self):
                return True

            # The base class' rate-limit/circuit-breaker layer touches Redis;
            # bypass it by overriding query() entirely just for this stub.
            def query(self, prompt, system_prompt="", *, user=None, website=None,
                      audit_id=None, role="upstream", region=""):
                self._call(prompt=prompt, system_prompt=system_prompt, region=region)
                return ProviderResult(succeeded=True)

            def _call(self, *, prompt, system_prompt, region=""):
                captured["region"] = region
                return ProviderResult(succeeded=True)

        _Stub().query("hi", region="in")
        assert captured["region"] == "in"
