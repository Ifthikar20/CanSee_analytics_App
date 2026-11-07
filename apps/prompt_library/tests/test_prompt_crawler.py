"""Tests for the prompt crawler service."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.prompt_library.models import PromptCrawlRun, PromptFanout
from apps.prompt_library.services.prompt_crawler import (
    _dedupe_fanouts,
    _llm_fanout,
    crawl_prompt,
)
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


def _prompt(*, text: str):
    """Prompt with a valid required industry (Prompt.industry is NOT NULL —
    bare Prompt.objects.create(text=...) violates the constraint)."""
    return PromptFactory(text=text)


def test_dedupe_fanouts_dedupes_caps_and_trims():
    out = _dedupe_fanouts([
        "Hijab stores Dallas",
        "hijab stores dallas",   # dup (case/space)
        "  best hijab shops  ",
        *[f"q{i}" for i in range(20)],
    ])
    # Deduped and capped at 8.
    assert len(out) == 8
    assert out[0] == "Hijab stores Dallas"
    assert "best hijab shops" in out  # trimmed


def test_llm_fanout_returns_empty_on_failure(monkeypatch):
    """No templated padding: a failed LLM call yields an empty fan-out."""
    from apps.prompt_library.services.prompt_crawler import _llm_fanout

    fake = MagicMock()
    fake.query.side_effect = RuntimeError("down")
    monkeypatch.setattr(
        "apps.llm_ranking.providers.claude.ClaudeProvider", lambda: fake,
    )
    assert _llm_fanout("some prompt", "Brand") == []


@pytest.mark.django_db
def test_crawl_prompt_sets_created_by_to_website_owner():
    """Regression: synchronous crawl path was creating audits without
    created_by, violating the not-null constraint on
    llm_ranking_audit.created_by_id."""
    website = WebsiteFactory()
    prompt = _prompt(text="best pizza app in dfw")

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[],
    ):
        crawl_prompt(website, prompt)

    audit = LLMRankingAudit.objects.filter(website=website).get()
    assert audit.created_by_id == website.user_id
    assert PromptCrawlRun.objects.filter(website=website, prompt=prompt).exists()


@pytest.mark.django_db
def test_crawl_prompt_queries_dict_shaped_variants():
    """Regression: list_model_variants() returns dicts, but the provider
    loop read them with getattr — always falsy on a dict — so every
    provider was skipped and scans silently queried no model at all."""
    from apps.llm_ranking.services.extraction_service import HaikuExtractionService
    from apps.prompt_library.tests.factories import PromptFactory

    website = WebsiteFactory()
    prompt = PromptFactory(text="best coffee in austin")

    fake_result = SimpleNamespace(succeeded=True, text="1. BrandX is great", error="")
    fake_provider = MagicMock()
    fake_provider.query.return_value = fake_result
    variants = [{
        "id": "claude:claude-sonnet", "provider": "claude",
        "model_id": "claude-sonnet", "label": "Claude",
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
        {"claude": lambda: fake_provider}, clear=True,
    ), patch(
        "apps.prompt_library.services.prompt_crawler._extract_brands",
        return_value=HaikuExtractionService._empty_result(),
    ):
        outcome = crawl_prompt(website, prompt)

    assert outcome.responses == 1
    fake_provider.query.assert_called_once()
    run = PromptCrawlRun.objects.get(website=website, prompt=prompt)
    assert run.status == PromptCrawlRun.STATUS_COMPLETE
    assert run.providers == ["claude"]
    assert LLMRankingResult.objects.filter(
        audit__website=website, provider="claude", query_succeeded=True,
    ).count() == 1


def _variant(provider="claude"):
    return [{
        "id": f"{provider}:m", "provider": provider, "model_id": "m",
        "label": provider.title(), "is_default": True, "configured": True,
    }]


@pytest.mark.django_db
def test_full_rerun_requeries_already_answered_models():
    """Regression: re-scans skipped every model that had ever answered,
    so a manual re-run (and every scheduled run) collected no fresh
    responses. The default is now a full re-run of all configured models."""
    from apps.llm_ranking.services.extraction_service import HaikuExtractionService
    from apps.prompt_library.tests.factories import PromptFactory

    website = WebsiteFactory()
    prompt = PromptFactory(text="rerun everything")

    fake_result = SimpleNamespace(succeeded=True, text="1. BrandX", error="")
    fake_provider = MagicMock()
    fake_provider.query.return_value = fake_result

    common = dict(
        _llm=patch("apps.prompt_library.services.prompt_crawler._llm_fanout", return_value=[]),
        _variants=patch("apps.prompt_library.services.prompt_crawler.list_model_variants",
                        return_value=_variant("claude")),
        _providers=patch.dict("apps.prompt_library.services.prompt_crawler.PROVIDERS",
                              {"claude": lambda: fake_provider}, clear=True),
        _extract=patch("apps.prompt_library.services.prompt_crawler._extract_brands",
                       return_value=HaikuExtractionService._empty_result()),
    )

    # First run answers with claude.
    with common["_llm"], common["_variants"], common["_providers"], common["_extract"]:
        crawl_prompt(website, prompt)
    assert LLMRankingResult.objects.filter(
        audit__website=website, provider="claude", query_succeeded=True,
    ).count() == 1

    # Full re-run queries claude AGAIN and adds a second run's row.
    with common["_llm"], common["_variants"], common["_providers"], common["_extract"]:
        outcome = crawl_prompt(website, prompt)
    assert outcome.responses == 1
    assert LLMRankingResult.objects.filter(
        audit__website=website, provider="claude", query_succeeded=True,
    ).count() == 2

    # Gap-fill mode skips the already-answered model entirely.
    fake_provider.query.reset_mock()
    with common["_llm"], common["_variants"], common["_providers"], common["_extract"]:
        outcome = crawl_prompt(website, prompt, only_missing=True)
    fake_provider.query.assert_not_called()
    assert outcome.responses == 0


@pytest.mark.django_db
def test_failed_crawl_always_records_a_reason():
    """Regression: a run that produced nothing failed with error='' and
    the UI showed 'unknown error' with nothing to debug."""
    from apps.prompt_library.tests.factories import PromptFactory

    website = WebsiteFactory()
    prompt = PromptFactory(text="silent failure prompt")

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[],
    ):
        crawl_prompt(website, prompt)

    run = PromptCrawlRun.objects.filter(website=website, prompt=prompt).latest("created_at")
    assert run.status == PromptCrawlRun.STATUS_FAILED
    assert run.error, "a failed run must explain why"


def test_llm_fanout_uses_system_prompt_kwarg(monkeypatch):
    """Regression: provider.query was being called with system= instead
    of system_prompt=, raising a TypeError that fell through to the
    heuristic fallback on every crawl."""
    fake_resp = MagicMock(text='["sub 1", "sub 2"]')
    fake_provider = MagicMock()
    fake_provider.query.return_value = fake_resp

    monkeypatch.setattr(
        "apps.llm_ranking.providers.claude.ClaudeProvider",
        lambda: fake_provider,
    )

    out = _llm_fanout("pizza ordering app in dfw", "AcmePizza")
    assert out == ["sub 1", "sub 2"]
    _, kwargs = fake_provider.query.call_args
    assert "system_prompt" in kwargs
    assert "system" not in kwargs


@pytest.mark.django_db
def test_crawl_replaces_fanouts_instead_of_appending(settings):
    """A re-scan replaces the prompt's fan-out set, so duplicates don't
    accumulate across runs."""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory()
    prompt = _prompt(text="best hijab store dallas")

    # Stale fan-outs from a previous run.
    PromptFanout.objects.create(website=website, prompt=prompt, text="old query 1")
    PromptFanout.objects.create(website=website, prompt=prompt, text="old query 2")

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=["fresh query a", "fresh query b", "fresh query a"],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[],
    ):
        crawl_prompt(website, prompt)

    texts = sorted(
        PromptFanout.objects.filter(website=website, prompt=prompt)
        .values_list("text", flat=True)
    )
    # Old set replaced; new set deduped.
    assert texts == ["fresh query a", "fresh query b"]


@pytest.mark.django_db
def test_crawl_prompt_runs_extraction_and_captures_competitors(settings):
    """The crawl must run the real extractor so competitor brand names
    in the response are captured, not just a substring check on the
    user's own brand."""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory(name="AcmePizza")
    prompt = _prompt(text="pizza ordering app in dfw")

    fake_result = SimpleNamespace(
        succeeded=True,
        text="The best options are Domino's, Pizza Hut, and AcmePizza.",
        error="",
    )
    fake_provider = MagicMock()
    fake_provider.query.return_value = fake_result

    extraction = {
        "is_mentioned": True,
        "mention_rank": 3,
        "sentiment": "positive",
        "mention_context": "and AcmePizza",
        "is_linked": False,
        "competitors_mentioned": [
            {"name": "Domino's", "position": 1, "linked": False},
            {"name": "Pizza Hut", "position": 2, "linked": False},
        ],
        "primary_recommendation": "Domino's",
        "citations": [],
        "confidence_score": 92.0,
        "extraction_model": "haiku",
        "extraction_version": "1",
    }

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[SimpleNamespace(configured=True, provider="claude")],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"claude": lambda: fake_provider},
    ), patch(
        "apps.llm_ranking.services.extraction_service.HaikuExtractionService.extract",
        return_value=extraction,
    ):
        crawl_prompt(website, prompt)

    row = LLMRankingResult.objects.get(audit__website=website, provider="claude")
    assert row.is_mentioned is True
    assert row.mention_rank == 3
    assert row.sentiment == "positive"
    names = {c["name"] for c in row.competitors_mentioned}
    assert names == {"Domino's", "Pizza Hut"}


@pytest.mark.django_db
def test_crawl_skips_models_already_answered(settings):
    """In gap-fill mode (only_missing=True) a model that already returned
    a response on a prior run is not re-queried, and the crawl still
    completes successfully. (Default crawls are full re-runs by design.)"""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory()
    prompt = _prompt(text="pizza ordering app in dfw")

    # Existing good response for claude from a previous run.
    LLMRankingResult.objects.create(
        audit=LLMRankingAudit.objects.create(
            website=website, created_by=website.user,
            status=LLMRankingAudit.STATUS_COMPLETED,
        ),
        provider="claude", prompt_index=0, prompt=prompt.text,
        source_prompt=prompt, response_text="prior answer",
        query_succeeded=True,
    )

    fake_provider = MagicMock()
    fake_provider.query.return_value = SimpleNamespace(
        succeeded=True, text="new answer", error="",
    )

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[SimpleNamespace(configured=True, provider="claude")],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"claude": lambda: fake_provider},
    ):
        outcome = crawl_prompt(website, prompt, only_missing=True)

    # claude was skipped — not queried again, no duplicate row created.
    fake_provider.query.assert_not_called()
    assert outcome.responses == 0
    assert LLMRankingResult.objects.filter(
        provider="claude", source_prompt=prompt,
    ).count() == 1
    run = PromptCrawlRun.objects.filter(website=website, prompt=prompt).latest("created_at")
    assert run.status == PromptCrawlRun.STATUS_COMPLETE


@pytest.mark.django_db
def test_crawl_retries_transient_failure_once_then_records(settings):
    """A transient provider failure is retried exactly once; a second
    failure is recorded and we move on."""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory()
    prompt = _prompt(text="pizza ordering app in dfw")

    fake_provider = MagicMock()
    fake_provider.query.side_effect = [
        SimpleNamespace(succeeded=False, text="", error="rate limit"),
        SimpleNamespace(succeeded=False, text="", error="rate limit"),
    ]

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[SimpleNamespace(configured=True, provider="claude")],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"claude": lambda: fake_provider},
    ):
        crawl_prompt(website, prompt)

    assert fake_provider.query.call_count == 2
    row = LLMRankingResult.objects.get(provider="claude", source_prompt=prompt)
    assert row.query_succeeded is False


@pytest.mark.django_db
def test_crawl_does_not_retry_missing_key(settings):
    """A service_unavailable result (missing key) is not retried."""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory()
    prompt = _prompt(text="pizza ordering app in dfw")

    fake_provider = MagicMock()
    fake_provider.query.return_value = SimpleNamespace(
        succeeded=False, text="",
        error="service_unavailable: gemini provider not enabled",
    )

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[SimpleNamespace(configured=True, provider="gemini")],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"gemini": lambda: fake_provider},
    ):
        crawl_prompt(website, prompt)

    assert fake_provider.query.call_count == 1


@pytest.mark.django_db
def test_crawl_prompt_records_failed_provider_row(settings):
    """A provider that returns succeeded=False gets a row flagged with
    the error, and does not count as a logged response."""
    settings.CITATION_EXTRACTION_ENABLED = False
    website = WebsiteFactory()
    prompt = _prompt(text="pizza ordering app in dfw")

    fake_result = SimpleNamespace(
        succeeded=False, text="",
        error="service_unavailable: gemini provider not enabled",
    )
    fake_provider = MagicMock()
    fake_provider.query.return_value = fake_result

    with patch(
        "apps.prompt_library.services.prompt_crawler._llm_fanout",
        return_value=[],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.list_model_variants",
        return_value=[SimpleNamespace(configured=True, provider="gemini")],
    ), patch(
        "apps.prompt_library.services.prompt_crawler.PROVIDERS",
        {"gemini": lambda: fake_provider},
    ):
        outcome = crawl_prompt(website, prompt)

    row = LLMRankingResult.objects.get(audit__website=website, provider="gemini")
    assert row.query_succeeded is False
    assert row.error_message.startswith("service_unavailable")
    assert outcome.responses == 0
