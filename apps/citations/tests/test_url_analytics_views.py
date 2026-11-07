"""API tests for the Sources > URLs dashboard endpoints."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.citations.services.extraction_service import extract_for_result
from apps.llm_ranking.models import LLMRankingResult
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.prompt_library.tests.factories import IndustryFactory, PromptFactory
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _seed(user):
    website = WebsiteFactory(user=user)
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    result = LLMRankingResultFactory(
        audit=audit,
        provider=LLMRankingResult.PROVIDER_PERPLEXITY,
        prompt_index=0,
        prompt="best treasury management software",
        citations=[
            {"url": "https://reddit.com/r/saas"},
            {"url": "https://techcrunch.com/x"},
        ],
        response_text="Some answer text.",
    )
    extract_for_result(str(result.id))
    return website, audit, result


class TestWebsiteUrls:
    def test_requires_auth(self):
        website = WebsiteFactory()
        client = APIClient()
        url = reverse("citations-website-urls", args=[website.id])
        assert client.get(url).status_code == 401

    def test_empty_website(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("citations-website-urls", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.data["total_retrievals"] == 0
        assert resp.data["unique_urls"] == 0
        assert resp.data["urls"] == []
        assert resp.data["url_types"] == []
        assert resp.data["movers"]["Top"] == []

    def test_aggregates_urls(self, auth_client):
        client, user = auth_client
        website, audit, _ = _seed(user)
        # Claude has no native citations field — its extractor strategy is
        # regex-over-prose, so the URL must appear in the response text.
        r2 = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE, prompt_index=1,
            prompt="top treasury tools",
            response_text="Community favorite: https://reddit.com/r/saas",
        )
        extract_for_result(str(r2.id))

        url = reverse("citations-website-urls", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.data
        assert body["total_retrievals"] == 3
        assert body["unique_urls"] == 2
        rows = {r["apex_domain"]: r for r in body["urls"]}
        assert rows["reddit.com"]["retrievals"] == 2
        assert set(rows["reddit.com"]["models"]) == {"perplexity", "claude"}
        assert rows["reddit.com"]["domain_type"] == "UGC"
        assert body["urls"][0]["retrievals"] >= body["urls"][1]["retrievals"]
        assert sum(t["count"] for t in body["url_types"]) == 3
        assert body["overview"]["series"]

    def test_tenant_isolation(self, auth_client):
        client, _ = auth_client
        other = UserFactory()
        other_website = WebsiteFactory(user=other)
        url = reverse("citations-website-urls", args=[other_website.id])
        assert client.get(url).status_code in (403, 404)


class TestAccurateMetrics:
    def _row(self, body, apex):
        return {r["apex_domain"]: r for r in body["urls"]}[apex]

    def test_citation_rate_from_native_markers(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        # Native source at position 0 -> referenced in prose as "[1]".
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt="p", citations=[{"url": "https://a.com/x"}],
            response_text="According to the data [1], savings grow.",
        )
        extract_for_result(str(r.id))
        body = client.get(reverse("citations-website-urls", args=[website.id])).data
        row = self._row(body, "a.com")
        assert row["retrievals"] == 1
        assert row["citations"] == 1
        assert row["citation_rate"] == 1.0

    def test_retrieved_but_not_cited_is_zero(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        # Source retrieved but never referenced in the prose.
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt="p", citations=[{"url": "https://a.com/x"}],
            response_text="A generic answer with no markers.",
        )
        extract_for_result(str(r.id))
        body = client.get(reverse("citations-website-urls", args=[website.id])).data
        row = self._row(body, "a.com")
        assert row["retrievals"] == 1
        assert row["citations"] == 0
        assert row["citation_rate"] == 0.0

    def test_citation_rate_is_bounded_cited_share(self, auth_client):
        """Multiple prose references still count as ONE cited retrieval:
        citation_rate is the share of retrievals that were cited, never
        a mean that can exceed 1.0."""
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE, prompt_index=0,
            prompt="p",
            response_text="See https://b.com/y and again https://b.com/y here.",
        )
        extract_for_result(str(r.id))
        body = client.get(reverse("citations-website-urls", args=[website.id])).data
        row = self._row(body, "b.com")
        assert row["retrievals"] == 1
        assert row["citations"] == 2
        assert row["citation_rate"] == 1.0

    def test_gap_score_competitors_times_usage(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(
            user=user, competitors=[{"name": "Acme", "domain": "acme.com"}],
        )
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        # Same source cited in two answers; both mention tracked competitor Acme.
        for idx in range(2):
            r = LLMRankingResultFactory(
                audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=idx,
                prompt=f"p{idx}", citations=[{"url": "https://src.com/x"}],
                response_text="", competitors_mentioned=[{"name": "Acme", "position": 1}],
            )
            extract_for_result(str(r.id))
        body = client.get(reverse("citations-website-urls", args=[website.id])).data
        row = self._row(body, "src.com")
        assert row["retrievals"] == 2
        assert row["gap_score"] == 2  # 1 distinct competitor x 2 retrievals

    def test_gap_score_zero_without_tracked_competitor(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user, competitors=[])
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt="p", citations=[{"url": "https://src.com/x"}],
            response_text="", competitors_mentioned=[{"name": "Unknown Co"}],
        )
        extract_for_result(str(r.id))
        body = client.get(reverse("citations-website-urls", args=[website.id])).data
        row = self._row(body, "src.com")
        assert row["gap_score"] == 0


class TestChatDetail:
    def test_returns_full_conversation(self, auth_client):
        client, user = auth_client
        website, audit, result = _seed(user)
        url = reverse("citations-website-chat-detail", args=[website.id, result.public_id])
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.data
        assert body["result_id"] == str(result.public_id)
        assert body["model"] == "perplexity"
        assert body["prompt"] == "best treasury management software"
        assert body["response_text"] == "Some answer text."
        # Both seeded citations show up as sources.
        domains = {s["apex_domain"] for s in body["sources"]}
        assert {"reddit.com", "techcrunch.com"} <= domains

    def test_brands_include_competitors(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt="p", citations=[{"url": "https://a.com/x"}], response_text="ans",
            is_mentioned=True, mention_rank=1,
            competitors_mentioned=[{"name": "Acme", "position": 3}],
        )
        extract_for_result(str(r.id))
        url = reverse("citations-website-chat-detail", args=[website.id, r.public_id])
        body = client.get(url).data
        names = {b["name"] for b in body["brands"]}
        assert website.name in names
        assert "Acme" in names

    def test_unknown_or_foreign_chat_404(self, auth_client):
        client, user = auth_client
        other = UserFactory()
        _, _, foreign = _seed(other)
        website = WebsiteFactory(user=user)
        url = reverse("citations-website-chat-detail", args=[website.id, foreign.public_id])
        assert client.get(url).status_code == 404


class TestTopicScoping:
    def test_topics_listed_and_filtered(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)

        industry = IndustryFactory(name="ALi")
        prompt = PromptFactory(industry=industry, text="best ai tools to organize life")
        # Result whose prompt is linked to the "ALi" topic.
        r1 = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt=prompt.text, source_prompt=prompt,
            citations=[{"url": "https://youtube.com/watch?v=1"}], response_text="",
        )
        # Result with no linked prompt -> no topic. Claude extracts via
        # regex, so the URL must appear in the prose.
        r2 = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE, prompt_index=1,
            prompt="unlinked prompt", source_prompt=None,
            response_text="Discussed at https://reddit.com/r/x today.",
        )
        extract_for_result(str(r1.id))
        extract_for_result(str(r2.id))

        url = reverse("citations-website-urls", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        topics = {t["name"]: t["count"] for t in resp.data["topics"]}
        assert topics.get("ALi") == 1
        assert resp.data["unique_urls"] == 2  # unfiltered

        scoped = client.get(url, {"topic": "ALi"})
        assert scoped.data["selected_topic"] == "ALi"
        assert scoped.data["unique_urls"] == 1
        assert scoped.data["urls"][0]["apex_domain"] == "youtube.com"

    def test_detail_topic_column_uses_industry_name(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        industry = IndustryFactory(name="ALi")
        prompt = PromptFactory(industry=industry, text="organize my life prompt")
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
            prompt=prompt.text, source_prompt=prompt,
            citations=[{"url": "https://youtube.com/watch?v=2"}], response_text="",
        )
        extract_for_result(str(r.id))
        url = reverse("citations-website-url-detail", args=[website.id])
        resp = client.get(url, {"url": "https://youtube.com/watch?v=2"})
        assert resp.status_code == 200
        assert resp.data["prompts"][0]["topic"] == "ALi"


class TestWebsiteUrlDetail:
    def test_missing_url_param(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("citations-website-url-detail", args=[website.id])
        assert client.get(url).status_code == 400

    def test_unknown_url_404(self, auth_client):
        client, user = auth_client
        website, _, _ = _seed(user)
        url = reverse("citations-website-url-detail", args=[website.id])
        resp = client.get(url, {"url": "https://nope.example.com/x"})
        assert resp.status_code == 404

    def test_detail_payload(self, auth_client):
        client, user = auth_client
        website, audit, _ = _seed(user)
        url = reverse("citations-website-url-detail", args=[website.id])
        resp = client.get(url, {"url": "https://reddit.com/r/saas"})
        assert resp.status_code == 200
        body = resp.data
        assert body["metrics"]["retrievals"]["value"] == 1
        assert body["metrics"]["domain_type"] == "UGC"
        assert "labels" in body["retrievals_over_time"]
        assert any(m["model"] == "perplexity" for m in body["retrievals_by_model"])
        assert len(body["prompts"]) == 1
        assert isinstance(body["chats"], list)


class TestResponseBrandExtraction:
    def test_extracts_bold_numbered_list(self):
        from apps.citations.services.url_analytics import extract_response_brands
        text = "1. **Domino's Pizza**\n   desc\n2. **Pizza Hut**\n   desc\n"
        names = [b["name"] for b in extract_response_brands(text)]
        assert names == ["Domino's Pizza", "Pizza Hut"]

    def test_dashed_lists_are_ignored_by_design(self):
        # The extractor only reads numbered lists (see _LIST_ITEM and the
        # extract_response_brands docstring): dash bullets carry no rank
        # signal, so they are deliberately not treated as brand rankings.
        from apps.citations.services.url_analytics import extract_response_brands
        text = "- Qapital — automates savings.\n- Digit — analyzes spending."
        assert extract_response_brands(text) == []

    def test_empty_text(self):
        from apps.citations.services.url_analytics import extract_response_brands
        assert extract_response_brands("") == []
        assert extract_response_brands("a paragraph with no list") == []

    def test_chat_detail_surfaces_listed_brands(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        r = LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_GPT4, prompt_index=0,
            prompt="pizza ordering app in dfw",
            response_text="1. **Domino's Pizza**\n2. **Pizza Hut**\n3. **Papa John's**",
            citations=[],
        )
        extract_for_result(str(r.id))
        url = reverse("citations-website-chat-detail", args=[website.id, r.public_id])
        body = client.get(url).data
        names = {b["name"] for b in body["brands"]}
        assert {"Domino's Pizza", "Pizza Hut", "Papa John's"} <= names


def test_extract_response_brands_filters_labels_and_subbullets():
    from apps.citations.services.url_analytics import extract_response_brands

    text = (
        "1. **Nalli Silk Sarees**\n"
        "   - **Description**: Known for premium silk.\n"
        "2. **Mehndi Couture**\n"
        "   - **Description**: Ethnic wear specialist.\n"
        "3. **India Bazaar**\n"
        "   - **Price**: Affordable.\n"
    )
    names = [b["name"] for b in extract_response_brands(text)]
    assert names == ["Nalli Silk Sarees", "Mehndi Couture", "India Bazaar"]
    assert "Description" not in names
    assert "Price" not in names
