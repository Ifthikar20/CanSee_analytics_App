"""Tests for LLM Ranking API views."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit
from apps.llm_ranking.tests.factories import LLMRankingAuditFactory, LLMRankingResultFactory
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def auth_client():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


@pytest.mark.django_db
class TestLLMRankingAuditListView:
    def test_list_empty(self, auth_client):
        client, user, website = auth_client
        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/")
        assert response.status_code == 200
        assert response.json()["data"]["count"] == 0

    def test_list_returns_audits(self, auth_client):
        client, user, website = auth_client
        LLMRankingAuditFactory(website=website, created_by=user)
        LLMRankingAuditFactory(website=website, created_by=user)
        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/")
        assert response.status_code == 200
        assert response.json()["data"]["count"] == 2

    def test_list_excludes_other_websites(self, auth_client):
        client, user, website = auth_client
        other_website = WebsiteFactory()
        LLMRankingAuditFactory(website=other_website)
        LLMRankingAuditFactory(website=website, created_by=user)
        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/")
        assert response.json()["data"]["count"] == 1

    def test_create_audit_queues_task(self, auth_client):
        client, user, website = auth_client
        with patch("apps.llm_ranking.tasks.run_llm_ranking_audit.delay") as mock_task, \
             patch("apps.llm_ranking.services.ranking_service.LLMRankingService.generate_prompts",
                   return_value=["What is the best SaaS tool?"]):
            response = client.post(
                f"/api/v1/llm-ranking/{website.id}/audits/",
                {
                    "business_name": "Acme SaaS",
                    "industry": "SaaS",
                    "keywords": ["analytics", "dashboard"],
                    "business_description": "Growth analytics platform",
                },
                format="json",
            )
        assert response.status_code == 202
        data = response.json()["data"]
        assert data["business_name"] == "Acme SaaS"
        assert data["status"] == "pending"
        mock_task.assert_called_once()

    def test_create_audit_with_custom_prompts(self, auth_client):
        client, user, website = auth_client
        with patch("apps.llm_ranking.tasks.run_llm_ranking_audit.delay"):
            response = client.post(
                f"/api/v1/llm-ranking/{website.id}/audits/",
                {
                    "business_name": "Acme SaaS",
                    "industry": "SaaS",
                    "custom_prompts": ["Find me a SaaS tool", "Best analytics software?"],
                },
                format="json",
            )
        assert response.status_code == 202
        # Verify prompts were stored
        audit_id = response.json()["data"]["id"]
        audit = LLMRankingAudit.objects.get(id=audit_id)
        assert "Find me a SaaS tool" in audit.prompts

    def test_create_audit_unauthenticated(self, auth_client):
        _, _, website = auth_client
        anon = APIClient()
        response = anon.post(f"/api/v1/llm-ranking/{website.id}/audits/", {})
        assert response.status_code == 401

    def test_create_audit_missing_required_fields(self, auth_client):
        """When neither the request nor the website has an industry, return 400."""
        client, user, website = auth_client
        website.industry = ""
        website.save(update_fields=["industry"])
        response = client.post(
            f"/api/v1/llm-ranking/{website.id}/audits/",
            {},
            format="json",
        )
        assert response.status_code == 400

    def test_create_audit_falls_back_to_website_fields(self, auth_client):
        """business_name/industry/keywords default to the Website row when omitted."""
        client, user, website = auth_client
        website.name = "Acme Corp"
        website.industry = "SaaS"
        website.description = "We build pipelines."
        website.topics = ["pipelines", "data"]
        website.save()

        with patch("apps.llm_ranking.tasks.run_llm_ranking_audit.delay"):
            response = client.post(
                f"/api/v1/llm-ranking/{website.id}/audits/",
                {"custom_prompts": ["Test prompt"]},
                format="json",
            )
        assert response.status_code == 202
        data = response.json()
        assert data["business_name"] == "Acme Corp"
        assert data["industry"] == "SaaS"


@pytest.mark.django_db
class TestLLMRankingAuditDetailView:
    def test_get_audit_with_results(self, auth_client):
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED,
            overall_score=72,
        )
        LLMRankingResultFactory(audit=audit, provider="claude", is_mentioned=True)
        LLMRankingResultFactory(audit=audit, provider="gpt4", is_mentioned=False)

        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["overall_score"] == 72
        assert len(data["results"]) == 2

    def test_get_audit_not_found(self, auth_client):
        client, user, website = auth_client
        import uuid
        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/{uuid.uuid4()}/")
        assert response.status_code == 404

    def test_delete_audit(self, auth_client):
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(website=website, created_by=user)
        response = client.delete(f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/")
        assert response.status_code == 204
        assert not LLMRankingAudit.objects.filter(id=audit.id).exists()

    def test_cannot_access_other_users_audit(self, auth_client):
        client, user, website = auth_client
        other_website = WebsiteFactory()
        audit = LLMRankingAuditFactory(website=other_website)
        response = client.get(f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/")
        assert response.status_code == 404


@pytest.mark.django_db
class TestLLMRankingProviderBreakdownView:
    def test_breakdown_groups_by_provider(self, auth_client):
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED,
        )
        LLMRankingResultFactory(audit=audit, provider="claude", is_mentioned=True, mention_rank=1)
        LLMRankingResultFactory(audit=audit, provider="claude", is_mentioned=False)
        LLMRankingResultFactory(audit=audit, provider="gpt4", is_mentioned=True, mention_rank=2)

        response = client.get(
            f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/breakdown/"
        )
        assert response.status_code == 200
        breakdown = response.json()["data"]
        providers = {b["provider"]: b for b in breakdown}

        assert "claude" in providers
        assert providers["claude"]["total_prompts"] == 2
        assert providers["claude"]["mentioned"] == 1
        assert providers["claude"]["mention_rate"] == 50.0

        assert "gpt4" in providers
        assert providers["gpt4"]["mentioned"] == 1
        assert providers["gpt4"]["avg_rank"] == 2.0


@pytest.mark.django_db
class TestLLMRankingRecommendationsView:
    def test_recommendations_returned_for_completed_audit(self, auth_client):
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED,
            overall_score=40,
            mention_rate=30.0,
        )
        response = client.get(
            f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/recommendations/"
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert "recommendations" in data
        assert isinstance(data["recommendations"], list)
        assert len(data["recommendations"]) >= 1

    def test_recommendations_blocked_for_pending_audit(self, auth_client):
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_PENDING,
        )
        response = client.get(
            f"/api/v1/llm-ranking/{website.id}/audits/{audit.id}/recommendations/"
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestLLMRankingHistoryView:
    def test_history_returns_completed_audits_only(self, auth_client):
        client, user, website = auth_client
        LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=60,
        )
        LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_PENDING, overall_score=0,
        )
        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["overall_score"] == 60
        assert data[0]["providers"] == []

    def test_history_includes_per_provider_stats(self, auth_client):
        from apps.llm_ranking.models import LLMRankingResult

        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=75,
        )
        # Claude: 2 of 2 mentions, ranks 1 and 3 (avg 2.0)
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=True, mention_rank=1,
        )
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=True, mention_rank=3,
        )
        # GPT-4: 1 of 2 mentioned
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_GPT4,
            query_succeeded=True, is_mentioned=True, mention_rank=2,
        )
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_GPT4,
            query_succeeded=True, is_mentioned=False, mention_rank=None,
        )
        # Failed queries must be excluded from per-provider stats
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_GEMINI,
            query_succeeded=False, is_mentioned=False, mention_rank=None,
        )

        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1

        providers = {p["provider"]: p for p in data[0]["providers"]}
        assert set(providers.keys()) == {"claude", "gpt4"}

        assert providers["claude"]["succeeded"] == 2
        assert providers["claude"]["mentioned"] == 2
        assert providers["claude"]["mention_rate"] == 100.0
        assert providers["claude"]["avg_rank"] == 2.0

        assert providers["gpt4"]["succeeded"] == 2
        assert providers["gpt4"]["mentioned"] == 1
        assert providers["gpt4"]["mention_rate"] == 50.0
        assert providers["gpt4"]["avg_rank"] == 2.0

    def test_history_aggregates_competitors_per_audit(self, auth_client):
        """The history endpoint should aggregate competitors_mentioned across
        results and return per-audit competitor visibility."""
        from apps.llm_ranking.models import LLMRankingResult

        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=70,
        )
        # 4 successful queries; competitor "Mixpanel" appears in 3, "Heap" in 1.
        for _ in range(3):
            LLMRankingResultFactory(
                audit=audit,
                provider=LLMRankingResult.PROVIDER_CLAUDE,
                query_succeeded=True, is_mentioned=True, mention_rank=1,
                competitors_mentioned=[
                    {"name": "Mixpanel", "position": 2, "linked": False},
                ],
            )
        LLMRankingResultFactory(
            audit=audit,
            provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=False, mention_rank=None,
            competitors_mentioned=[
                {"name": "Heap", "position": 1, "linked": False},
                {"name": "Mixpanel", "position": 3, "linked": True},
            ],
        )

        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1

        competitors = {c["name"]: c for c in data[0]["competitors"]}
        # Mixpanel appeared in all 4 prompts: visibility = 100%
        assert competitors["Mixpanel"]["mention_count"] == 4
        assert competitors["Mixpanel"]["visibility"] == 100.0
        # Heap appeared in 1 of 4 prompts: visibility = 25%
        assert competitors["Heap"]["mention_count"] == 1
        assert competitors["Heap"]["visibility"] == 25.0
        # Sorted by mention_count desc
        assert data[0]["competitors"][0]["name"] == "Mixpanel"

    def test_history_computes_citation_share(self, auth_client):
        """citation_share = self mentions / (self + competitor mentions)."""
        from apps.llm_ranking.models import LLMRankingResult

        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=70,
        )
        # 2 prompts where we're mentioned, with 0 and 1 competitors respectively.
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=True, mention_rank=1,
            competitors_mentioned=[],
        )
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=True, mention_rank=2,
            competitors_mentioned=[
                {"name": "Mixpanel", "position": 1},
            ],
        )
        # 1 prompt where we're not mentioned but competitor is.
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=False, mention_rank=None,
            competitors_mentioned=[
                {"name": "Heap", "position": 1},
                {"name": "Amplitude", "position": 2},
            ],
        )
        # Failed query — must be excluded.
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=False, is_mentioned=False, mention_rank=None,
            competitors_mentioned=[],
        )

        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        data = response.json()["data"]
        # self_mentions=2, total brand mentions = 2 + 1 + 2 = 5 -> 40%
        assert data[0]["citation_self"] == 2
        assert data[0]["citation_total"] == 5
        assert data[0]["citation_share"] == 40.0

    def test_history_returns_zero_citation_share_when_no_brands_mentioned(self, auth_client):
        from apps.llm_ranking.models import LLMRankingResult
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=10,
        )
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=False, mention_rank=None,
            competitors_mentioned=[],
        )
        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        data = response.json()["data"]
        assert data[0]["citation_share"] == 0.0
        assert data[0]["competitors"] == []

    def test_history_dedupes_competitor_within_a_response(self, auth_client):
        """If a single response lists the same competitor twice, it should
        only count once toward that prompt's visibility (avoid double-count
        when LLMs accidentally repeat names)."""
        from apps.llm_ranking.models import LLMRankingResult
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            website=website, created_by=user,
            status=LLMRankingAudit.STATUS_COMPLETED, overall_score=50,
        )
        LLMRankingResultFactory(
            audit=audit, provider=LLMRankingResult.PROVIDER_CLAUDE,
            query_succeeded=True, is_mentioned=False, mention_rank=None,
            competitors_mentioned=[
                {"name": "Mixpanel", "position": 1},
                {"name": "Mixpanel", "position": 5},   # duplicate within same response
            ],
        )
        response = client.get(f"/api/v1/llm-ranking/{website.id}/history/")
        data = response.json()["data"]
        comp = next(c for c in data[0]["competitors"] if c["name"] == "Mixpanel")
        assert comp["mention_count"] == 1
        assert comp["visibility"] == 100.0  # 1 of 1 prompts


@pytest.mark.django_db
class TestVisibilityOverviewView:
    def test_requires_auth(self):
        website = WebsiteFactory()
        resp = APIClient().get(f"/api/v1/llm-ranking/{website.id}/visibility-overview/")
        assert resp.status_code == 401

    def test_empty_state(self, auth_client):
        client, _user, website = auth_client
        resp = client.get(f"/api/v1/llm-ranking/{website.id}/visibility-overview/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_data"] is False
        assert data["brand_current"] == 0.0
        assert data["competitor_current"] == 0.0
        assert len(data["labels"]) == 12

    def test_populated_state(self, auth_client):
        from django.utils import timezone
        client, user, website = auth_client
        LLMRankingAuditFactory(
            created_by=user, website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=58.0, completed_at=timezone.now(),
        )
        resp = client.get(f"/api/v1/llm-ranking/{website.id}/visibility-overview/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_data"] is True
        assert data["brand_current"] == 58.0

    def test_excludes_other_websites(self, auth_client):
        from django.utils import timezone
        client, user, website = auth_client
        other = WebsiteFactory(user=user)
        LLMRankingAuditFactory(
            created_by=user, website=other,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=80.0, completed_at=timezone.now(),
        )
        resp = client.get(f"/api/v1/llm-ranking/{website.id}/visibility-overview/")
        data = resp.json()
        assert data["brand_current"] == 0.0

    def test_response_includes_per_competitor_breakdown(self, auth_client):
        from django.utils import timezone
        client, user, website = auth_client
        audit = LLMRankingAuditFactory(
            created_by=user, website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=40.0, completed_at=timezone.now(),
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=["Mixpanel", "Amplitude"],
        )
        LLMRankingResultFactory(
            audit=audit, query_succeeded=True,
            competitors_mentioned=["Mixpanel"],
        )
        resp = client.get(f"/api/v1/llm-ranking/{website.id}/visibility-overview/")
        data = resp.json()
        assert "competitors" in data
        assert len(data["competitors"]) == 2
        first = data["competitors"][0]
        assert set(first) >= {"name", "series", "current", "delta_pct"}
        assert first["name"] == "Mixpanel"
        assert first["current"] == 100.0
