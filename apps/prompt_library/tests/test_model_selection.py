"""Per-prompt model selection: BrandPrompt.model_variants drives scans.

A prompt with an explicit selection queries exactly those variants (several
variants of one provider allowed) via get_provider_for_variant; the default
path (empty selection) keeps the one-default-model-per-provider behaviour.
Gap-fill judges "already answered" at provider:model granularity for
selected prompts. The create serializer validates variant ids against the
live catalog.
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.services.prompt_crawler import crawl_prompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


def _fake_provider(model_id):
    fake = MagicMock()
    fake.model = model_id
    fake.query.return_value = MagicMock(
        succeeded=True, text=f"1. BrandX via {model_id}", error="", citations=[],
    )
    return fake


def _empty_extraction():
    from apps.llm_ranking.services.extraction_service import HaikuExtractionService
    return HaikuExtractionService._empty_result()


@pytest.mark.django_db
class TestCrawlWithModelSelection:
    def test_selected_variants_run_and_stamp_model_id(self):
        website = WebsiteFactory()
        prompt = PromptFactory(text="best coffee in austin")
        BrandPrompt.objects.create(
            website=website, prompt=prompt,
            model_variants=["claude:claude-sonnet-5", "gpt4:gpt-5.6-terra"],
        )

        providers = {
            "claude:claude-sonnet-5": _fake_provider("claude-sonnet-5"),
            "gpt4:gpt-5.6-terra": _fake_provider("gpt-5.6-terra"),
        }
        with patch(
            "apps.prompt_library.services.prompt_crawler._llm_fanout",
            return_value=[],
        ), patch(
            "apps.prompt_library.services.prompt_crawler.get_provider_for_variant",
            side_effect=lambda vid: providers.get(vid),
        ), patch(
            "apps.prompt_library.services.prompt_crawler.list_model_variants",
        ) as default_registry, patch(
            "apps.prompt_library.services.prompt_crawler._extract_brands",
            return_value=_empty_extraction(),
        ):
            outcome = crawl_prompt(website, prompt)

        assert outcome.responses == 2
        # The default registry path is bypassed entirely.
        default_registry.assert_not_called()
        rows = LLMRankingResult.objects.filter(audit__website=website)
        assert sorted(rows.values_list("provider", "model_id")) == [
            ("claude", "claude-sonnet-5"), ("gpt4", "gpt-5.6-terra"),
        ]

    def test_two_variants_of_one_provider_both_run(self):
        website = WebsiteFactory()
        prompt = PromptFactory(text="best crm for smb")
        BrandPrompt.objects.create(
            website=website, prompt=prompt,
            model_variants=["gpt4:gpt-5.6-sol", "gpt4:gpt-4o-mini"],
        )
        providers = {
            "gpt4:gpt-5.6-sol": _fake_provider("gpt-5.6-sol"),
            "gpt4:gpt-4o-mini": _fake_provider("gpt-4o-mini"),
        }
        with patch(
            "apps.prompt_library.services.prompt_crawler._llm_fanout",
            return_value=[],
        ), patch(
            "apps.prompt_library.services.prompt_crawler.get_provider_for_variant",
            side_effect=lambda vid: providers.get(vid),
        ), patch(
            "apps.prompt_library.services.prompt_crawler._extract_brands",
            return_value=_empty_extraction(),
        ):
            outcome = crawl_prompt(website, prompt)

        assert outcome.responses == 2
        assert sorted(
            LLMRankingResult.objects
            .filter(audit__website=website, provider="gpt4")
            .values_list("model_id", flat=True)
        ) == ["gpt-4o-mini", "gpt-5.6-sol"]

    def test_gap_fill_skips_variant_that_already_answered(self):
        website = WebsiteFactory()
        prompt = PromptFactory(text="best bank for llc")
        BrandPrompt.objects.create(
            website=website, prompt=prompt,
            model_variants=["claude:claude-sonnet-5", "gpt4:gpt-5.6-terra"],
        )
        # claude-sonnet-5 answered on a previous run.
        audit = LLMRankingAudit.objects.create(
            website=website, created_by=website.user, business_name="B",
            status=LLMRankingAudit.STATUS_COMPLETED, prompts=[prompt.text],
        )
        LLMRankingResult.objects.create(
            audit=audit, provider="claude", model_id="claude-sonnet-5",
            prompt=prompt.text, source_prompt=prompt,
            response_text="answered", query_succeeded=True,
        )

        providers = {"gpt4:gpt-5.6-terra": _fake_provider("gpt-5.6-terra")}
        calls = []
        with patch(
            "apps.prompt_library.services.prompt_crawler._llm_fanout",
            return_value=[],
        ), patch(
            "apps.prompt_library.services.prompt_crawler.get_provider_for_variant",
            side_effect=lambda vid: calls.append(vid) or providers.get(vid),
        ), patch(
            "apps.prompt_library.services.prompt_crawler._extract_brands",
            return_value=_empty_extraction(),
        ):
            outcome = crawl_prompt(website, prompt, only_missing=True)

        assert outcome.responses == 1
        # The answered variant was never even instantiated.
        assert calls == ["gpt4:gpt-5.6-terra"]

    def test_empty_selection_uses_default_registry(self):
        website = WebsiteFactory()
        prompt = PromptFactory(text="best pizza in dallas")
        BrandPrompt.objects.create(website=website, prompt=prompt)  # no selection

        fake = _fake_provider("claude-haiku-4-5")
        variants = [{
            "id": "claude:claude-haiku-4-5", "provider": "claude",
            "model_id": "claude-haiku-4-5", "label": "Haiku",
            "is_default": True, "configured": True,
        }]
        with patch(
            "apps.prompt_library.services.prompt_crawler._llm_fanout",
            return_value=[],
        ), patch(
            "apps.prompt_library.services.prompt_crawler.list_model_variants",
            return_value=variants,
        ), patch.dict(
            "apps.prompt_library.services.prompt_crawler.PROVIDERS",
            {"claude": lambda: fake}, clear=True,
        ), patch(
            "apps.prompt_library.services.prompt_crawler._extract_brands",
            return_value=_empty_extraction(),
        ):
            outcome = crawl_prompt(website, prompt)

        assert outcome.responses == 1
        row = LLMRankingResult.objects.get(audit__website=website)
        assert (row.provider, row.model_id) == ("claude", "claude-haiku-4-5")


class TestCreateSerializerModels:
    def test_known_variants_pass_and_dedupe(self):
        from apps.prompt_library.api.v1.serializers import PromptCreateSerializer
        s = PromptCreateSerializer(data={
            "text": "any alternative to rocket money",
            "models": [
                "claude:claude-sonnet-5", "claude:claude-sonnet-5",
                "gpt4:gpt-5.6-terra",
            ],
        })
        assert s.is_valid(), s.errors
        assert s.validated_data["models"] == [
            "claude:claude-sonnet-5", "gpt4:gpt-5.6-terra",
        ]

    def test_unknown_variant_rejected(self):
        from apps.prompt_library.api.v1.serializers import PromptCreateSerializer
        s = PromptCreateSerializer(data={
            "text": "any alternative to rocket money",
            "models": ["gpt4:gpt-2"],
        })
        assert not s.is_valid()
        assert "models" in s.errors
