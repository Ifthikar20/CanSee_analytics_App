"""Tests for the shared dashboard result filters and filter options.

apply_result_filters is the single home for pill-filter semantics; the
overview, KPI, breakdown and deep-dive builders all delegate to it.
"""
import pytest

from apps.llm_ranking.models import LLMRankingResult
from apps.llm_ranking.services._window import apply_result_filters, parse_csv_filter
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import IndustryFactory, PromptFactory
from apps.websites.tests.factories import WebsiteFactory


class TestParseCsvFilter:
    def test_none_passthrough(self):
        assert parse_csv_filter(None) is None

    def test_csv_string(self):
        assert parse_csv_filter("a, b ,c") == ["a", "b", "c"]

    def test_repeated_params(self):
        assert parse_csv_filter(["a", "b,c"]) == ["a", "b", "c"]

    def test_empty_yields_none(self):
        assert parse_csv_filter("") is None
        assert parse_csv_filter([",", " "]) is None


@pytest.mark.django_db
class TestApplyResultFilters:
    def _base_qs(self, audit):
        return LLMRankingResult.objects.filter(audit=audit)

    def test_provider_filter(self):
        audit = LLMRankingAuditFactory()
        LLMRankingResultFactory(audit=audit, provider="claude", prompt_index=0)
        LLMRankingResultFactory(audit=audit, provider="gpt4", prompt_index=1)

        qs = apply_result_filters(self._base_qs(audit), providers=["gpt4"])
        assert [r.provider for r in qs] == ["gpt4"]

    def test_topic_filter_via_source_prompt_industry(self):
        audit = LLMRankingAuditFactory()
        fintech = IndustryFactory(name="Fintech")
        other = IndustryFactory(name="Retail")
        p1 = PromptFactory(industry=fintech)
        p2 = PromptFactory(industry=other)
        LLMRankingResultFactory(audit=audit, source_prompt=p1, prompt_index=0)
        LLMRankingResultFactory(audit=audit, source_prompt=p2, prompt_index=1)
        LLMRankingResultFactory(audit=audit, source_prompt=None, prompt_index=2)

        qs = apply_result_filters(self._base_qs(audit), topics=["Fintech"])
        assert [r.source_prompt_id for r in qs] == [p1.id]

    def test_tag_filter_scoped_to_audit_website(self):
        website = WebsiteFactory()
        other_site = WebsiteFactory()
        audit = LLMRankingAuditFactory(website=website)
        prompt = PromptFactory()

        # Same Prompt row saved on two websites; only the audit's own
        # website carries the tag. The filter must not leak across tenants.
        BrandPrompt.objects.create(website=other_site, prompt=prompt, tags=["branded"])
        LLMRankingResultFactory(audit=audit, source_prompt=prompt, prompt_index=0)

        qs = apply_result_filters(self._base_qs(audit), tags=["branded"])
        assert qs.count() == 0

        BrandPrompt.objects.create(website=website, prompt=prompt, tags=["branded"])
        qs = apply_result_filters(self._base_qs(audit), tags=["branded"])
        assert qs.count() == 1

    def test_tag_join_does_not_duplicate_rows(self):
        website = WebsiteFactory()
        audit = LLMRankingAuditFactory(website=website)
        prompt = PromptFactory()
        BrandPrompt.objects.create(
            website=website, prompt=prompt, tags=["branded", "priority"],
        )
        LLMRankingResultFactory(audit=audit, source_prompt=prompt, prompt_index=0)

        qs = apply_result_filters(
            self._base_qs(audit), tags=["branded", "priority"],
        )
        assert qs.count() == 1


@pytest.mark.django_db
class TestFilterOptions:
    def test_options_reflect_window_results(self):
        from django.utils import timezone

        from apps.llm_ranking.models import LLMRankingAudit
        from apps.llm_ranking.services.overview_stats import build_filter_options

        website = WebsiteFactory()
        audit = LLMRankingAuditFactory(
            website=website, created_by=website.user,
            status=LLMRankingAudit.STATUS_COMPLETED, completed_at=timezone.now(),
        )
        industry = IndustryFactory(name="Fintech")
        prompt = PromptFactory(industry=industry)
        BrandPrompt.objects.create(website=website, prompt=prompt, tags=["branded"])
        LLMRankingResultFactory(
            audit=audit, provider="gpt4", source_prompt=prompt, prompt_index=0,
        )
        LLMRankingResultFactory(
            audit=audit, provider="claude", source_prompt=None, prompt_index=1,
        )

        options = build_filter_options(website.user)
        assert {m["value"] for m in options["models"]} == {"claude", "gpt4"}
        assert options["topics"] == [{"name": "Fintech", "count": 1}]
        assert options["tags"] == [{"name": "branded", "count": 1}]
        # Results without a source_prompt link are counted, not hidden:
        # they fall outside tag/topic filters by construction.
        assert options["unlinked_results"] == 1

    def test_empty_account_returns_empty_options(self):
        from apps.llm_ranking.services.overview_stats import build_filter_options

        user = WebsiteFactory().user
        options = build_filter_options(user)
        assert options == {
            "models": [], "tags": [], "topics": [], "unlinked_results": 0,
        }
