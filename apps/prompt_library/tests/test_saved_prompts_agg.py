"""Tests for the saved-prompts table payload (WebsiteSavedPromptsAggView).

Focus is the per-prompt rollups the table renders: competitors, citations,
per-engine visibility and the sentiment distribution. All of it is built in
the single pass over LLMRankingResult, so these also guard against someone
narrowing the .only() and silently emptying the columns.
"""

import itertools

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.prompt_library.api.v1.views import _citation_domains, _competitor_names
from apps.prompt_library.models import BrandPrompt, Industry, Prompt
from apps.websites.tests.factories import WebsiteFactory

PROMPT_TEXT = "best crm for small business"


@pytest.fixture
def setup(db):
    user = UserFactory()
    website = WebsiteFactory(user=user, name="Acme CRM")
    industry = Industry.objects.create(name="SaaS", slug="saas")
    prompt = Prompt.objects.create(
        industry=industry, text=PROMPT_TEXT, text_hash="h1",
        intent_bucket="comparison", style="comparison",
        demand_score=0.8, effectiveness_score=0.65,
    )
    BrandPrompt.objects.create(website=website, prompt=prompt, tags=["core"])
    audit = LLMRankingAudit.objects.create(website=website, created_by=user)
    return user, website, prompt, audit


_seq = itertools.count()


def _result(audit, **kw):
    # (audit, prompt_index, provider, model_id, run_id) is unique_together,
    # so every row needs its own index.
    defaults = {
        "prompt": PROMPT_TEXT,
        "prompt_index": next(_seq),
        "response_text": "...",
        "is_mentioned": False,
    }
    defaults.update(kw)
    return LLMRankingResult.objects.create(audit=audit, **defaults)


def _fetch(user, website):
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/saved-prompts/agg/"
    )
    assert resp.status_code == 200, resp.content
    # EnvelopeRenderer wraps every <400 response as {success, data}.
    return resp.json()["data"]


# -- helpers ------------------------------------------------------------------


def test_competitor_names_tolerates_both_json_shapes():
    """The column has been written by several extractor generations."""
    assert _competitor_names([
        "Salesforce",
        {"name": "HubSpot", "position": 2},
        {"brand": "Pipedrive"},
        {"name": "   "},
        {"noname": 1},
        42,
    ]) == ["Salesforce", "HubSpot", "Pipedrive"]


def test_competitor_names_survives_a_non_list():
    assert _competitor_names(None) == []
    assert _competitor_names({"name": "x"}) == []


def test_citation_domains_normalises_and_rejects_bad_schemes():
    out = _citation_domains([
        "https://www.g2.com/categories/crm?utm_source=x",
        {"url": "https://reddit.com/r/smallbusiness/"},
        {"link": "https://g2.com/other-page"},
        "javascript:alert(1)",
        "not a url",
        {},
    ])
    # www. is stripped so the two g2 URLs roll up to one domain.
    assert out == ["g2.com", "reddit.com", "g2.com"]


# -- payload ------------------------------------------------------------------


@pytest.mark.django_db
def test_rollups_are_built_from_results(setup):
    user, website, _prompt, audit = setup

    _result(audit, provider="claude", is_mentioned=True, mention_rank=1,
            sentiment="positive",
            competitors_mentioned=[{"name": "HubSpot"}, "Salesforce"],
            citations=["https://www.g2.com/crm", "https://reddit.com/r/x/"])
    _result(audit, provider="claude", is_mentioned=True, mention_rank=3,
            sentiment="neutral",
            competitors_mentioned=["HubSpot"],
            citations=[{"url": "https://g2.com/crm"}])
    # Not mentioned: competitors and citations here still count, because a
    # prompt we are absent from is exactly the one worth acting on.
    _result(audit, provider="gpt4", is_mentioned=False,
            competitors_mentioned=["HubSpot", "Pipedrive"],
            citations=["https://capterra.com/crm"])

    row = _fetch(user, website)["rows"][0]

    assert row["responses_seen"] == 3
    assert row["total_mentions"] == 2
    assert row["visibility_pct"] == 67

    # Competitors, most-named first.
    assert row["top_competitors"][0] == {"name": "HubSpot", "count": 3}
    assert row["competitors_count"] == 3
    assert {c["name"] for c in row["top_competitors"]} == {
        "HubSpot", "Salesforce", "Pipedrive",
    }

    # Citations roll up by normalised domain.
    assert row["citations_count"] == 4
    assert row["top_domains"][0] == {"domain": "g2.com", "count": 2}

    # Per-engine visibility separates "everywhere" from "one engine only".
    by_engine = {e["provider"]: e for e in row["by_engine"]}
    assert by_engine["claude"] == {
        "provider": "claude", "responses": 2, "mentioned": 2, "visibility_pct": 100,
    }
    assert by_engine["gpt4"]["visibility_pct"] == 0

    # Distribution behind the sentiment mean.
    assert row["sentiment_dist"] == {"positive": 1, "neutral": 1, "negative": 0}

    # Free field already on the select_related Prompt.
    assert row["effectiveness_score"] == pytest.approx(0.65)


@pytest.mark.django_db
def test_unrun_prompt_returns_empty_rollups_not_nulls(setup):
    """The table renders these without guards, so shape must be stable."""
    user, website, _prompt, _audit = setup

    row = _fetch(user, website)["rows"][0]

    assert row["top_competitors"] == []
    assert row["competitors_count"] == 0
    assert row["citations_count"] == 0
    assert row["top_domains"] == []
    assert row["by_engine"] == []
    assert row["sentiment_dist"] == {"positive": 0, "neutral": 0, "negative": 0}
    assert row["visibility_pct"] == 0
    assert row["sentiment_score"] is None


@pytest.mark.django_db
def test_malformed_json_columns_do_not_break_the_row(setup):
    user, website, _prompt, audit = setup
    _result(audit, provider="claude", is_mentioned=True,
            competitors_mentioned={"not": "a list"},
            citations="also not a list")

    row = _fetch(user, website)["rows"][0]

    assert row["top_competitors"] == []
    assert row["citations_count"] == 0
    assert row["total_mentions"] == 1


@pytest.mark.django_db
def test_results_for_other_prompts_do_not_leak_in(setup):
    user, website, _prompt, audit = setup
    _result(audit, prompt="something else entirely", provider="claude",
            is_mentioned=True, competitors_mentioned=["Ghost"])

    row = _fetch(user, website)["rows"][0]

    assert row["responses_seen"] == 0
    assert row["top_competitors"] == []
