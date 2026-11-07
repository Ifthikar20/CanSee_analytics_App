"""HTTP-mode behavior of the internal-service facades.

The suite runs in-process by default (config/settings/test.py pins the
service URLs empty); these tests flip individual settings to point at
fake service hosts and assert the facades translate correctly: bearer
auth sent, envelopes unwrapped, usage recorded, failures degrade to
the same shapes the scan pipeline already handles.
"""

from __future__ import annotations

import pytest
import responses

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.citations.services import content_reader, source_sentiment, web_search
from apps.websites.tests.factories import WebsiteFactory

INTEL = "http://intelligence.test"
SOURCES = "http://sources.test"


@pytest.mark.django_db
class TestIntelligenceFacadeHTTP:
    @responses.activate
    def test_analyze_content_unwraps_and_records_usage(self, settings):
        settings.INTELLIGENCE_SERVICE_URL = INTEL
        settings.INTELLIGENCE_AUTH_TOKEN = "tok"
        user = UserFactory()
        website = WebsiteFactory(user=user)
        envelope = {
            "result": {"brands": [{"name": "X"}], "target_brand_present": False,
                       "relevant_to_query": True, "summary": "s"},
            "usage": {"input_tokens": 9, "output_tokens": 5},
            "duration_ms": 120,
            "model": "claude-haiku-4-5",
            "prompt_version": "v2",
        }
        responses.post(f"{INTEL}/v1/analyze-content", json=envelope)

        out = source_sentiment.analyze_content(
            "text", query="q", target_brand="X", website=website, user=user,
        )

        assert out == envelope["result"]
        sent = responses.calls[0].request
        assert sent.headers["Authorization"] == "Bearer tok"
        row = AITokenUsage.objects.get()
        assert row.module == "source_sentiment"
        assert row.input_tokens == 9
        assert row.output_tokens == 5

    @responses.activate
    def test_analyze_content_service_down_degrades(self, settings):
        settings.INTELLIGENCE_SERVICE_URL = INTEL
        responses.post(f"{INTEL}/v1/analyze-content", status=502)
        out = source_sentiment.analyze_content("text", query="q", target_brand="X")
        assert out == {"brands": [], "error": "intelligence_unavailable"}
        assert AITokenUsage.objects.count() == 0

    @responses.activate
    def test_serp_relevance_converts_string_keys(self, settings):
        settings.INTELLIGENCE_SERVICE_URL = INTEL
        envelope = {
            "result": {"1": {"relevant": True, "reason": ""},
                       "2": {"relevant": False, "reason": "handbags"}},
            "usage": None, "duration_ms": 0, "model": "m", "prompt_version": "v2",
        }
        responses.post(f"{INTEL}/v1/serp-relevance", json=envelope)
        items = [{"rank": 1, "title": "a", "url": "u"},
                 {"rank": 2, "title": "b", "url": "v"}]
        out = source_sentiment.assess_serp_relevance("q", items)
        assert out[2]["relevant"] is False
        assert set(out.keys()) == {1, 2}

    @responses.activate
    def test_serp_relevance_service_down_fails_open(self, settings):
        settings.INTELLIGENCE_SERVICE_URL = INTEL
        responses.post(f"{INTEL}/v1/serp-relevance", status=502)
        items = [{"rank": 1, "title": "a", "url": "u"}]
        out = source_sentiment.assess_serp_relevance("q", items)
        assert out == {1: {"relevant": True, "reason": ""}}


@pytest.mark.django_db
class TestSourcesFacadeHTTP:
    @responses.activate
    def test_search_web_passes_results_through(self, settings):
        settings.SOURCES_SERVICE_URL = SOURCES
        settings.SOURCES_AUTH_TOKEN = "tok"
        settings.PERPLEXITY_API_KEY = ""
        rows = [{"rank": 1, "url": "https://a.com", "domain": "a.com",
                 "title": "t", "snippet": "", "date": "", "last_updated": ""}]
        responses.post(f"{SOURCES}/v1/search", json={"results": rows})

        assert web_search.is_configured() is True
        out = web_search.search_web("best bagels")
        assert out == rows
        assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"

    @responses.activate
    def test_search_web_service_error_returns_empty(self, settings):
        settings.SOURCES_SERVICE_URL = SOURCES
        settings.PERPLEXITY_API_KEY = ""
        responses.post(f"{SOURCES}/v1/search", status=502)
        assert web_search.search_web("q") == []

    def test_search_web_quota_short_circuits_before_http(self, settings):
        settings.SOURCES_SERVICE_URL = SOURCES
        settings.PERPLEXITY_SEARCH_DAILY_LIMIT_PER_USER = 0
        # No responses.activate: an HTTP attempt would raise
        # ConnectionError, so returning [] proves the quota gate ran first.
        assert web_search.search_web("q", user_id="u1") == []

    @responses.activate
    def test_read_url_delegates_and_fails_soft(self, settings):
        settings.SOURCES_SERVICE_URL = SOURCES
        settings.SOURCES_AUTH_TOKEN = "tok"
        row = {"status": "ok", "kind": "reddit", "text": "t",
               "word_count": 1, "detail": ""}
        responses.post(f"{SOURCES}/v1/read", json=row)
        assert content_reader.read_url("https://reddit.com/r/x/comments/1/a/") == row

        responses.reset()
        responses.post(f"{SOURCES}/v1/read", status=503)
        out = content_reader.read_url("https://reddit.com/r/x/comments/1/a/")
        assert out["status"] == "error"
        assert "sources service unavailable" in out["detail"]
