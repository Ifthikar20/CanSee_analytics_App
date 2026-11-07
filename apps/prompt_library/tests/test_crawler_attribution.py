"""Attribution on crawl spend: acting user vs website owner (system).

Regression tests for the metering overhaul: crawl provider queries used
to run unattributed (user=None), which skipped the ledger AND the spend
wall, while extraction spend went to the website owner even when someone
else clicked the button.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.services.extraction_service import HaikuExtractionService
from apps.prompt_library.services.prompt_crawler import crawl_prompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


def _variants():
    return [{
        "id": "claude:m", "provider": "claude", "model_id": "m",
        "label": "Claude", "is_default": True, "configured": True,
    }]


def _crawl(website, prompt, fake_provider, **kwargs):
    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=_variants(),
    ), patch.dict(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"claude": lambda: fake_provider}, clear=True,
    ), patch(
        "apps.prompt_library.services.prompt_crawler._extract_brands",
        return_value=HaikuExtractionService._empty_result(),
    ):
        return crawl_prompt(website, prompt, **kwargs)


@pytest.mark.django_db
def test_manual_scan_attributes_spend_to_acting_user():
    website = WebsiteFactory()
    clicker = UserFactory(plan="individual")
    prompt = PromptFactory(text="best crm for dentists")

    fake_provider = MagicMock()
    fake_provider.query.return_value = SimpleNamespace(
        succeeded=True, text="1. BrandX", error="",
    )

    _crawl(website, prompt, fake_provider, acting_user=clicker)

    kwargs = fake_provider.query.call_args.kwargs
    assert kwargs["user"] == clicker
    assert kwargs["website"] == website
    assert kwargs["module"] == "prompt_library"
    assert kwargs["extra_metadata"] == {"actor": "user"}
    assert kwargs["audit_id"]


@pytest.mark.django_db
def test_scheduled_scan_attributes_owner_as_system():
    website = WebsiteFactory()
    prompt = PromptFactory(text="best crm for dentists")

    fake_provider = MagicMock()
    fake_provider.query.return_value = SimpleNamespace(
        succeeded=True, text="1. BrandX", error="",
    )

    _crawl(website, prompt, fake_provider)  # no acting_user = scheduler path

    kwargs = fake_provider.query.call_args.kwargs
    assert kwargs["user"] == website.user
    assert kwargs["extra_metadata"] == {"actor": "system"}


@pytest.mark.django_db
def test_extraction_spend_follows_acting_user():
    website = WebsiteFactory()
    clicker = UserFactory(plan="individual")
    prompt = PromptFactory(text="best crm for dentists")

    fake_provider = MagicMock()
    fake_provider.query.return_value = SimpleNamespace(
        succeeded=True, text="1. BrandX", error="",
    )

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=_variants(),
    ), patch.dict(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"claude": lambda: fake_provider}, clear=True,
    ), patch.object(
        HaikuExtractionService, "extract",
        return_value=HaikuExtractionService._empty_result(),
    ) as mock_extract:
        crawl_prompt(website, prompt, acting_user=clicker)

    assert mock_extract.call_args.kwargs["user"] == clicker
