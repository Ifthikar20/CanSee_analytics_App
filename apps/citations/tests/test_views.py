"""API tests for the citations endpoints."""
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
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_auto_extraction(settings):
    # pytest 8 rejects non-Mark objects in pytestmark, so the old
    # override_settings entry broke collection; the pytest-django settings
    # fixture is the supported equivalent.
    settings.CITATION_EXTRACTION_ENABLED = False


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _seed_audit_with_citations(user):
    website = WebsiteFactory(user=user)
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    result = LLMRankingResultFactory(
        audit=audit,
        provider=LLMRankingResult.PROVIDER_PERPLEXITY,
        prompt_index=0,
        citations=[
            {"url": "https://reddit.com/r/saas"},
            {"url": "https://techcrunch.com/x"},
        ],
        response_text="",
    )
    extract_for_result(str(result.id))
    return website, audit, result


class TestAuthGuard:
    def test_audit_citations_requires_auth(self):
        audit = LLMRankingAuditFactory()
        client = APIClient()
        url = reverse("citations-audit-list", args=[audit.id])
        resp = client.get(url)
        assert resp.status_code == 401


class TestAuditCitations:
    def test_lists_for_owner(self, auth_client):
        client, user = auth_client
        _, audit, _ = _seed_audit_with_citations(user)
        url = reverse("citations-audit-list", args=[audit.id])
        resp = client.get(url)
        assert resp.status_code == 200
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 2

    def test_filter_by_source_class(self, auth_client):
        client, user = auth_client
        _, audit, _ = _seed_audit_with_citations(user)
        url = reverse("citations-audit-list", args=[audit.id])
        resp = client.get(url, {"source_class": "reddit"})
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1
        assert results[0]["source_class"] == "reddit"

    def test_tenant_isolation(self, auth_client):
        client, user = auth_client
        # Audit belongs to a *different* user.
        other = UserFactory()
        _, audit, _ = _seed_audit_with_citations(other)
        url = reverse("citations-audit-list", args=[audit.id])
        resp = client.get(url)
        assert resp.status_code in (403, 404)


class TestAuditSourceInfluence:
    def test_returns_provider_breakdown(self, auth_client):
        client, user = auth_client
        _, audit, _ = _seed_audit_with_citations(user)
        url = reverse("citations-audit-influence", args=[audit.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.data["total_citations"] == 2
        assert "perplexity" in resp.data["providers"]


class TestWebsiteCitationsAndInfluence:
    def test_website_citations_list(self, auth_client):
        client, user = auth_client
        website, _, _ = _seed_audit_with_citations(user)
        url = reverse("citations-website-list", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 2

    def test_website_influence_empty(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("citations-website-influence", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.data["snapshots"] == []


class TestWebsiteSourceInfluenceAggregation:
    def test_empty_returns_zeros(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("citations-website-influence", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.data
        assert body["total_citations"] == 0
        assert body["unique_domains"] == 0
        assert body["your_site_share"] == 0
        assert body["competitor_share"] == 0
        assert body["breakdown"] == {}
        assert body["top_domains"] == []
        assert body["by_provider"] == {}
        assert body["snapshots"] == []
        assert "period_start" in body and "period_end" in body

    def test_aggregation_math(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        # 4 providers x 5 prompts x 3 citations each = 60 citations.
        providers = ["perplexity", "claude", "gpt-4", "gemini"]
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        for prov in providers:
            for idx in range(5):
                r = LLMRankingResultFactory(
                    audit=audit, provider=prov, prompt_index=idx,
                    citations=[
                        {"url": "https://reddit.com/r/x"},
                        {"url": "https://techcrunch.com/y"},
                        {"url": "https://en.wikipedia.org/wiki/z"},
                    ],
                    response_text="",
                )
                extract_for_result(str(r.id))
        url = reverse("citations-website-influence", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.data
        assert body["total_citations"] == 60
        assert body["unique_domains"] == 3
        assert body["breakdown"]["reddit"]["count"] == 20
        assert abs(body["breakdown"]["reddit"]["share"] - (20 / 60)) < 0.01
        # Per-provider shape
        assert set(body["by_provider"].keys()) == set(providers)
        for prov in providers:
            assert body["by_provider"][prov]["total_citations"] == 15
        # top_domains capped at 50, includes enriched fields
        assert len(body["top_domains"]) <= 50
        td = body["top_domains"][0]
        assert "source_class" in td
        assert "is_target" in td
        assert "is_competitor" in td

    def test_target_competitor_flagging_propagates(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user, url="https://mysite.com")
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        r = LLMRankingResultFactory(
            audit=audit, provider="perplexity", prompt_index=0,
            citations=[
                {"url": "https://mysite.com/a"},
                {"url": "https://reddit.com/b"},
            ],
            response_text="",
        )
        extract_for_result(str(r.id))
        # Force is_target/is_competitor on the rows for deterministic test.
        from apps.citations.models import Citation as CitationModel
        CitationModel.objects.filter(apex_domain="mysite.com").update(is_target=True)
        CitationModel.objects.filter(apex_domain="reddit.com").update(is_competitor=True)

        url = reverse("citations-website-influence", args=[website.id])
        resp = client.get(url)
        body = resp.data
        assert body["your_site_share"] > 0
        assert body["competitor_share"] > 0
        flags = {td["apex_domain"]: td for td in body["top_domains"]}
        assert flags["mysite.com"]["is_target"] is True
        assert flags["reddit.com"]["is_competitor"] is True

    def test_tenant_isolation(self, auth_client):
        client, user = auth_client
        other = UserFactory()
        other_website = WebsiteFactory(user=other)
        url = reverse("citations-website-influence", args=[other_website.id])
        resp = client.get(url)
        assert resp.status_code in (403, 404)


class TestSnapshotTopDomainsEnrichment:
    def test_top_domains_carries_source_class_and_flags(self):
        from apps.citations.models import Citation as CitationModel
        from apps.citations.services.snapshot_service import compute_for_website

        website = WebsiteFactory(url="https://mysite.com")
        audit = LLMRankingAuditFactory(website=website)
        r = LLMRankingResultFactory(
            audit=audit, provider="perplexity", prompt_index=0,
            citations=[
                {"url": "https://reddit.com/a"},
                {"url": "https://techcrunch.com/b"},
                {"url": "https://mysite.com/c"},
            ],
            response_text="",
        )
        extract_for_result(str(r.id))
        CitationModel.objects.filter(apex_domain="mysite.com").update(is_target=True)

        compute_for_website(website, period_days=30)
        from apps.citations.models import SourceInfluenceSnapshot as Snap
        snap = Snap.objects.get(website=website, provider="perplexity")
        by_domain = {d["apex_domain"]: d for d in snap.top_domains}
        assert by_domain["reddit.com"]["source_class"] == "reddit"
        assert by_domain["techcrunch.com"]["source_class"] == "news"
        assert by_domain["mysite.com"]["is_target"] is True


class TestGlobalInfluence:
    def test_returns_snapshots_payload(self, auth_client):
        client, _ = auth_client
        url = reverse("citations-global-influence")
        resp = client.get(url)
        assert resp.status_code == 200
        assert "snapshots" in resp.data
        assert "source_classes" in resp.data
