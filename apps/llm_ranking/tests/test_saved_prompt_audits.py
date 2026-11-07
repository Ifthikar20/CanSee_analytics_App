"""Tests for the saved-prompts-only audit sourcing.

Audits run exactly the list the user curated on the Prompts page
(BrandPrompt). Nothing is generated: an empty list is a refusal that
names the fix, never an invented substitute.
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.services.audit_runner import (
    NoSavedPromptsError,
    gather_saved_prompts,
)
from apps.prompt_library.models import BrandPrompt, RejectedBrandPrompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def auth_client():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


def _save(website, prompt):
    return BrandPrompt.objects.create(website=website, prompt=prompt)


@pytest.mark.django_db
class TestGatherSavedPrompts:
    def test_empty_list_raises(self):
        website = WebsiteFactory()
        with pytest.raises(NoSavedPromptsError):
            gather_saved_prompts(website, website.user)

    def test_returns_runner_shape_with_prompt_id(self):
        website = WebsiteFactory()
        prompt = PromptFactory(text="best analytics tools for startups?")
        _save(website, prompt)

        items = gather_saved_prompts(website, website.user)
        assert len(items) == 1
        item = items[0]
        assert item["text"] == "best analytics tools for startups?"
        # Runner reads "type", not "intent" — the library's own key.
        assert item["type"] == prompt.intent_bucket
        assert item["prompt_id"] == str(prompt.id)
        assert item["source_label"] == "Saved"

    def test_inactive_prompts_excluded(self):
        website = WebsiteFactory()
        _save(website, PromptFactory(is_active=False))
        with pytest.raises(NoSavedPromptsError):
            gather_saved_prompts(website, website.user)

    def test_rejected_prompts_excluded(self):
        website = WebsiteFactory()
        prompt = PromptFactory()
        _save(website, prompt)
        RejectedBrandPrompt.objects.create(website=website, prompt=prompt)
        with pytest.raises(NoSavedPromptsError):
            gather_saved_prompts(website, website.user)

    def test_rejection_scoped_to_its_own_website(self):
        website = WebsiteFactory()
        other = WebsiteFactory()
        prompt = PromptFactory()
        _save(website, prompt)
        RejectedBrandPrompt.objects.create(website=other, prompt=prompt)
        assert len(gather_saved_prompts(website, website.user)) == 1

    def test_variables_filled_from_website(self):
        website = WebsiteFactory(name="Acme Analytics")
        prompt = PromptFactory(
            text="best tools like Acme?",
            template_text="best tools like {{ company_name }}?",
        )
        _save(website, prompt)
        items = gather_saved_prompts(website, website.user)
        assert items[0]["text"] == "best tools like Acme Analytics?"

    def test_dedupes_on_normalised_text(self):
        website = WebsiteFactory()
        _save(website, PromptFactory(text="Best CRM tools?"))
        _save(website, PromptFactory(text="  best   crm tools?  "))
        items = gather_saved_prompts(website, website.user)
        assert len(items) == 1

    def test_cap_keeps_most_recent(self):
        website = WebsiteFactory()
        for i in range(3):
            _save(website, PromptFactory(text=f"prompt number {i}?"))
        with patch(
            "core.utils.constants.max_prompts_for_user", return_value=2,
        ):
            items = gather_saved_prompts(website, website.user)
        assert len(items) == 2
        # order_by -created_at keeps the newest saves.
        assert items[0]["text"] == "prompt number 2?"


@pytest.mark.django_db
class TestRunNowRefusal:
    def _make_schedule(self, user, website):
        from django.utils import timezone

        from apps.llm_ranking.models import LLMRankingSchedule
        return LLMRankingSchedule.objects.create(
            website=website, created_by=user, is_enabled=True,
            frequency="weekly", business_name=website.name or "Biz",
            industry="SaaS", next_run_at=timezone.now(),
        )

    def test_run_now_refuses_without_saved_prompts(self, auth_client):
        client, user, website = auth_client
        self._make_schedule(user, website)
        response = client.post(f"/api/v1/llm-ranking/{website.id}/schedule/run-now/")
        assert response.status_code == 400
        body = response.json()
        body = body.get("data", body)
        assert body["code"] == "no_saved_prompts"
        assert body["cta_to"].endswith("/prompts")

    def test_create_refuses_without_saved_prompts(self, auth_client):
        client, user, website = auth_client
        response = client.post(
            f"/api/v1/llm-ranking/{website.id}/audits/",
            {"business_name": "Biz", "industry": "SaaS"},
            format="json",
        )
        assert response.status_code == 400
        body = response.json()
        body = body.get("data", body)
        assert body["code"] == "no_saved_prompts"

    def test_run_now_uses_saved_prompts(self, auth_client):
        client, user, website = auth_client
        self._make_schedule(user, website)
        prompt = PromptFactory(text="what is the best crm?")
        _save(website, prompt)

        with patch("apps.llm_ranking.tasks.run_llm_ranking_audit.delay"):
            response = client.post(
                f"/api/v1/llm-ranking/{website.id}/schedule/run-now/",
            )
        assert response.status_code == 202

        from apps.llm_ranking.models import LLMRankingAudit
        audit = LLMRankingAudit.objects.get(website=website)
        assert audit.prompt_source == LLMRankingAudit.PROMPT_SOURCE_LIBRARY
        assert [p["text"] for p in audit.prompts] == ["what is the best crm?"]
        assert audit.prompts[0]["prompt_id"] == str(prompt.id)

    def test_preview_returns_saved_list_or_empty_code(self, auth_client):
        client, user, website = auth_client
        response = client.get(f"/api/v1/llm-ranking/{website.id}/preview-prompts/")
        assert response.status_code == 200
        body = response.json()
        body = body.get("data", body)
        assert body["prompts"] == []
        assert body["code"] == "no_saved_prompts"

        _save(website, PromptFactory(text="saved question?"))
        response = client.get(f"/api/v1/llm-ranking/{website.id}/preview-prompts/")
        body = response.json()
        body = body.get("data", body)
        assert [p["text"] for p in body["prompts"]] == ["saved question?"]


@pytest.mark.django_db
class TestBeatSkip:
    def test_dispatch_skips_and_bumps_without_failure(self):
        from django.utils import timezone

        from apps.llm_ranking.models import LLMRankingSchedule
        from apps.llm_ranking.tasks import dispatch_scheduled_audits

        website = WebsiteFactory()
        schedule = LLMRankingSchedule.objects.create(
            website=website, created_by=website.user, is_enabled=True,
            frequency="weekly", business_name="Biz", industry="SaaS",
            next_run_at=timezone.now(),
        )
        dispatch_scheduled_audits()
        schedule.refresh_from_db()

        # An empty saved list is user state, not an error: the run is
        # re-scheduled and the auto-pause counter stays untouched.
        assert schedule.next_run_at > timezone.now()
        assert schedule.consecutive_failures == 0
        from apps.llm_ranking.models import LLMRankingAudit
        assert not LLMRankingAudit.objects.filter(website=website).exists()
