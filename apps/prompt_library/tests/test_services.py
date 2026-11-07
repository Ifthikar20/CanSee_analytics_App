"""Service-layer tests for the prompt library."""
from __future__ import annotations

from unittest import mock

import pytest

from apps.llm_ranking.services.audit_runner import gather_prompts
from apps.llm_ranking.tests.factories import LLMRankingAuditFactory
from apps.prompt_library.models import IntentBucket, PromptSampleEntry
from apps.prompt_library.services.dedup_service import is_near_duplicate
from apps.prompt_library.services.sampler_service import sample_prompts_for_audit
from apps.prompt_library.tests.factories import IndustryFactory, PromptFactory


@pytest.mark.django_db
def test_sampler_stratifies_across_buckets():
    industry = IndustryFactory()
    # Three category prompts, one comparison, no problem/local — sampler
    # must back-fill from leftovers when a bucket runs short.
    for _i in range(3):
        PromptFactory(industry=industry, intent_bucket=IntentBucket.CATEGORY)
    PromptFactory(industry=industry, intent_bucket=IntentBucket.COMPARISON)

    audit = LLMRankingAuditFactory()
    sample_run = sample_prompts_for_audit(
        audit_run=audit, industry=industry, n=4, seed=1
    )
    entries = list(PromptSampleEntry.objects.filter(sample_run=sample_run))
    assert len(entries) == 4
    buckets = {e.prompt.intent_bucket for e in entries}
    assert IntentBucket.CATEGORY in buckets
    assert IntentBucket.COMPARISON in buckets


@pytest.mark.django_db
def test_sampler_is_reproducible_under_fixed_seed():
    industry = IndustryFactory()
    for _ in range(20):
        PromptFactory(industry=industry)

    a1 = LLMRankingAuditFactory()
    a2 = LLMRankingAuditFactory()
    r1 = sample_prompts_for_audit(audit_run=a1, industry=industry, n=10, seed=42)
    r2 = sample_prompts_for_audit(audit_run=a2, industry=industry, n=10, seed=42)

    ids_1 = list(
        PromptSampleEntry.objects.filter(sample_run=r1).order_by("rank").values_list("prompt_id", flat=True)
    )
    ids_2 = list(
        PromptSampleEntry.objects.filter(sample_run=r2).order_by("rank").values_list("prompt_id", flat=True)
    )
    assert ids_1 == ids_2


@pytest.mark.django_db
def test_dedup_rejects_near_duplicate():
    industry = IndustryFactory()
    PromptFactory(industry=industry, text="What are the best CRM tools for startups?")
    # Same text → trigram fallback returns 1.0 similarity.
    with mock.patch("apps.prompt_library.services.dedup_service._embed", return_value=None):
        assert is_near_duplicate("What are the best CRM tools for startups?", industry) is True
        assert is_near_duplicate("Completely unrelated cooking question", industry) is False


@pytest.mark.django_db
def test_audit_runner_uses_library_when_prompt_source_is_library():
    industry = IndustryFactory()
    p = PromptFactory(industry=industry, text="library prompt one")
    audit = LLMRankingAuditFactory(prompts=["vault prompt"])
    sample_prompts_for_audit(audit_run=audit, industry=industry, n=1, seed=1)
    audit.prompt_source = "library"
    audit.save()

    items = gather_prompts(audit)
    texts = [i["text"] for i in items]
    assert p.text in texts
    assert "vault prompt" not in texts
