"""API tests for the prompt_library endpoints."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.tests.factories import LLMRankingAuditFactory
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import IndustryFactory, PromptFactory
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def auth():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


@pytest.mark.django_db
def test_industries_requires_auth():
    anon = APIClient()
    assert anon.get("/api/v1/prompt-library/industries/").status_code == 401


@pytest.mark.django_db
def test_industries_list(auth):
    client, _, _ = auth
    IndustryFactory(name="SaaS / Analytics", slug="saas-analytics")
    IndustryFactory(is_active=False)
    resp = client.get("/api/v1/prompt-library/industries/")
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()]
    assert "SaaS / Analytics" in names
    assert len(names) == 1  # inactive excluded


@pytest.mark.django_db
def test_prompts_list_filter_by_industry(auth):
    client, _, _ = auth
    industry = IndustryFactory(slug="crm")
    other = IndustryFactory(slug="other")
    PromptFactory(industry=industry, text="how to pick a CRM?")
    PromptFactory(industry=other, text="unrelated")
    resp = client.get("/api/v1/prompt-library/prompts/?industry=crm")
    assert resp.status_code == 200
    body = resp.json()
    rows = body.get("data", body).get("results", body) if isinstance(body, dict) else body
    # Tolerate either a paginated envelope or a flat list.
    if isinstance(rows, dict) and "results" in rows:
        rows = rows["results"]
    assert any("CRM" in r["text"] for r in rows)


@pytest.mark.django_db
def test_preview_sample_returns_prompts(auth):
    client, _, _ = auth
    industry = IndustryFactory()
    for _ in range(5):
        PromptFactory(industry=industry)
    resp = client.post(
        "/api/v1/prompt-library/prompts/preview-sample/",
        {"industry_id": str(industry.id), "n": 3},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["prompts"]) == 3
    assert body["industry"]["id"] == str(industry.id)


@pytest.mark.django_db
def test_use_library_sample_persists_run(auth):
    client, user, website = auth
    industry = IndustryFactory()
    for _ in range(5):
        PromptFactory(industry=industry)
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    resp = client.post(
        f"/api/v1/prompt-library/audits/{audit.id}/use-library-sample/",
        {"industry_id": str(industry.id), "n": 3, "seed": 7},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["seed"] == 7
    assert len(body["entries"]) == 3
    audit.refresh_from_db()
    assert audit.prompt_source == "library"


@pytest.mark.django_db
def test_get_audit_sample_returns_null_when_missing(auth):
    client, user, website = auth
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    resp = client.get(f"/api/v1/prompt-library/audits/{audit.id}/sample/")
    assert resp.status_code == 200
    assert resp.json()["sample_run"] is None


@pytest.mark.django_db
def test_add_and_list_brand_prompt(auth):
    client, _, website = auth
    industry = IndustryFactory(slug="food-bev")
    p = PromptFactory(industry=industry, text="Cooking bread pudding at home")
    add = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/brand-prompts/",
        {"prompt_id": str(p.id)},
        format="json",
    )
    assert add.status_code == 201
    body = add.json()
    bp_id = body.get("id") or body.get("data", {}).get("id")
    assert bp_id

    listing = client.get(f"/api/v1/prompt-library/websites/{website.id}/brand-prompts/")
    assert listing.status_code == 200
    rows = listing.json()
    rows = rows.get("data", rows) if isinstance(rows, dict) else rows
    assert any(r["prompt"]["id"] == str(p.id) for r in rows)


@pytest.mark.django_db
def test_add_brand_prompt_idempotent(auth):
    client, _, website = auth
    p = PromptFactory()
    first = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/brand-prompts/",
        {"prompt_id": str(p.id)},
        format="json",
    )
    second = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/brand-prompts/",
        {"prompt_id": str(p.id)},
        format="json",
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert BrandPrompt.objects.filter(website=website, prompt=p).count() == 1


@pytest.mark.django_db
def test_remove_brand_prompt(auth):
    client, _, website = auth
    p = PromptFactory()
    add = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/brand-prompts/",
        {"prompt_id": str(p.id)},
        format="json",
    )
    bp_id = add.json().get("id") or add.json().get("data", {}).get("id")
    delete = client.delete(f"/api/v1/prompt-library/brand-prompts/{bp_id}/")
    assert delete.status_code == 204
    assert not BrandPrompt.objects.filter(id=bp_id).exists()


@pytest.mark.django_db
def test_brand_prompt_other_user_blocked():
    industry = IndustryFactory()
    p = PromptFactory(industry=industry)
    other_user = UserFactory()
    other_website = WebsiteFactory(user=other_user)

    intruder = UserFactory()
    client = APIClient()
    client.force_authenticate(user=intruder)
    resp = client.post(
        f"/api/v1/prompt-library/websites/{other_website.id}/brand-prompts/",
        {"prompt_id": str(p.id)},
        format="json",
    )
    assert resp.status_code in (403, 404)


@pytest.mark.django_db
def test_create_prompt_with_new_topic(auth):
    client, _, website = auth
    # Need at least one active industry so the no-topic default path exists,
    # but here we pass an explicit new topic name.
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {"text": "What are the best budgeting apps in 2026?", "topic": "ALi"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body.get("brand_prompt_id")
    # A BrandPrompt linked to a Prompt filed under the new "ALi" topic.
    bp = BrandPrompt.objects.select_related("prompt__industry").get(
        id=body["brand_prompt_id"],
    )
    assert bp.website_id == website.id
    assert bp.prompt.industry.name == "ALi"
    assert bp.prompt.text == "What are the best budgeting apps in 2026?"


@pytest.mark.django_db
def test_create_prompt_reuses_existing_topic(auth):
    client, _, website = auth
    industry = IndustryFactory(name="ALi", slug="ali")
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {"text": "How do I automate cash flow?", "topic": "ALi"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    bp = BrandPrompt.objects.select_related("prompt__industry").get(
        id=resp.json()["brand_prompt_id"],
    )
    assert bp.prompt.industry_id == industry.id


@pytest.mark.django_db
def test_create_prompt_requires_text(auth):
    client, _, website = auth
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {"topic": "ALi"},
        format="json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_prompt_detail_returns_recent_chats(auth):
    from apps.citations.services.extraction_service import extract_for_result
    from apps.llm_ranking.models import LLMRankingResult
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory

    client, user, website = auth
    industry = IndustryFactory(name="ALi", slug="ali")
    p = PromptFactory(industry=industry, text="best budgeting apps in 2026")
    BrandPrompt.objects.create(website=website, prompt=p)
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    r = LLMRankingResultFactory(
        audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
        prompt="best budgeting apps in 2026", response_text="Here are some apps...",
        is_mentioned=True, mention_rank=2,
        citations=[{"url": "https://reddit.com/r/x"}],
    )
    extract_for_result(str(r.id))

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["prompt"]["topic"] == "ALi"
    chats = body["recent_chats"]
    assert len(chats) == 1
    chat = chats[0]
    assert chat["result_id"] == str(r.public_id)
    assert chat["brand_mentioned"] is True
    assert chat["position"] == 2
    assert chat["models"] == ["perplexity"]
    assert "reddit.com" in chat["sources"]


@pytest.mark.django_db
def test_prompt_detail_includes_latest_scan(auth):
    from apps.prompt_library.models import Prompt, PromptCrawlRun

    client, _user, website = auth
    p = Prompt.objects.create(text="how to track LLM visibility")
    BrandPrompt.objects.create(website=website, prompt=p)

    PromptCrawlRun.objects.create(
        website=website, prompt=p,
        status=PromptCrawlRun.STATUS_FAILED,
        providers=["claude"], error="rate_limit_exceeded",
    )
    latest = PromptCrawlRun.objects.create(
        website=website, prompt=p,
        status=PromptCrawlRun.STATUS_RUNNING,
        providers=["claude", "gpt4"],
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["latest_scan"]["id"] == str(latest.id)
    assert body["latest_scan"]["status"] == PromptCrawlRun.STATUS_RUNNING
    assert body["latest_scan"]["providers"] == ["claude", "gpt4"]


@pytest.mark.django_db
def test_prompt_detail_heals_stale_running_scan(auth):
    """A run stuck in 'running' well past the staleness window is flipped
    to failed on read, so the UI stops showing a forever-running scan."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.prompt_library.models import Prompt, PromptCrawlRun

    client, _user, website = auth
    p = Prompt.objects.create(text="pizza ordering app in dfw")
    BrandPrompt.objects.create(website=website, prompt=p)

    run = PromptCrawlRun.objects.create(
        website=website, prompt=p,
        status=PromptCrawlRun.STATUS_RUNNING,
        started_at=timezone.now() - timedelta(minutes=45),
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["latest_scan"]["status"] == PromptCrawlRun.STATUS_FAILED

    run.refresh_from_db()
    assert run.status == PromptCrawlRun.STATUS_FAILED
    assert run.completed_at is not None


@pytest.mark.django_db
def test_prompt_detail_keeps_fresh_running_scan(auth):
    """A recently-started run is left as running."""
    from django.utils import timezone

    from apps.prompt_library.models import Prompt, PromptCrawlRun

    client, _user, website = auth
    p = Prompt.objects.create(text="pizza ordering app in dfw")
    BrandPrompt.objects.create(website=website, prompt=p)

    PromptCrawlRun.objects.create(
        website=website, prompt=p,
        status=PromptCrawlRun.STATUS_RUNNING,
        started_at=timezone.now(),
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["latest_scan"]["status"] == PromptCrawlRun.STATUS_RUNNING


@pytest.mark.django_db
def test_prompt_detail_latest_scan_none_when_never_crawled(auth):
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="another prompt")
    BrandPrompt.objects.create(website=website, prompt=p)

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["latest_scan"] is None


def test_match_brand_domain():
    from apps.prompt_library.api.v1.views import _match_brand_domain

    domains = ["tasselsbyrenu.com", "indiabazaar.com", "reddit.com", "yelp.com"]
    assert _match_brand_domain("Tassels By Renu", domains) == "tasselsbyrenu.com"
    assert _match_brand_domain("India Bazaar", domains) == "indiabazaar.com"
    # No plausible match -> None (UI falls back to name lookup / badge).
    assert _match_brand_domain("Krishna Sarees", domains) is None
    assert _match_brand_domain("", domains) is None


@pytest.mark.django_db
def test_prompt_detail_attaches_logo_from_cited_domain(auth):
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best saree shop in dallas")
    BrandPrompt.objects.create(website=website, prompt=p)

    r = LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        response_text="Try Tassels By Renu.",
        competitors_mentioned=[{"name": "Tassels By Renu", "position": 1}],
        citations=["https://tasselsbyrenu.com/shop"],
    )
    from apps.citations.services.extraction_service import extract_for_result
    extract_for_result(str(r.id))

    body = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    brand = next(b for b in body["brands"] if b["name"] == "Tassels By Renu")
    assert brand["domain"] == "tasselsbyrenu.com"


@pytest.mark.django_db
def test_prompt_detail_prefers_llm_supplied_domain(auth):
    """When the extractor supplies a brand's website domain, it's used for
    the logo crawl in preference to citation matching - and never shown."""
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best running shoes")
    BrandPrompt.objects.create(website=website, prompt=p)

    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        response_text="Nike makes great shoes.",
        competitors_mentioned=[
            {"name": "Nike", "position": 1, "domain": "nike.com"},
        ],
    )

    body = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    brand = next(b for b in body["brands"] if b["name"] == "Nike")
    assert brand["domain"] == "nike.com"
    # The raw LLM-domain helper key must not leak into the payload.
    assert "_llm_domain" not in brand


@pytest.mark.django_db
def test_prompt_detail_position_follows_response_list_order(auth):
    """Position reflects the order brands appear in the response, not the
    extractor's noisy positions - so a single listing yields distinct
    ranks (1, 2, 3...) instead of collapsing several brands to #1."""
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best saree shop in dallas")
    BrandPrompt.objects.create(website=website, prompt=p)

    response = (
        "Top saree shops in Dallas:\n"
        "1. **Tassels By Renu**\n"
        "2. **Kohinoor Fashion**\n"
        "3. **India Bazaar**\n"
    )
    # Extractor assigned the same noisy position (1) to several brands.
    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        response_text=response,
        competitors_mentioned=[
            {"name": "Tassels By Renu", "position": 1},
            {"name": "Kohinoor Fashion", "position": 1},
            {"name": "India Bazaar", "position": 1},
        ],
    )

    body = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    brands = {b["name"]: b for b in body["brands"]}
    assert brands["Tassels By Renu"]["avg_position"] == 1
    assert brands["Kohinoor Fashion"]["avg_position"] == 2
    assert brands["India Bazaar"]["avg_position"] == 3


@pytest.mark.django_db
def test_prompt_detail_uses_per_competitor_sentiment(auth):
    """Competitor sentiment captured by the extractor surfaces in the
    brand table, not just the tracked brand's sentiment."""
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best vintage shop in dallas")
    BrandPrompt.objects.create(website=website, prompt=p)

    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        extraction_model="claude-haiku-4-5", extraction_version="v2",
        competitors_mentioned=[
            {"name": "Vintage Martini", "position": 1, "sentiment": "positive"},
            {"name": "Thrift Town", "position": 2, "sentiment": "negative"},
            {"name": "Plain Mention", "position": 3},  # no sentiment -> neutral
        ],
    )

    body = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    brands = {b["name"]: b for b in body["brands"]}
    # positive -> 85, negative -> 25, neutral (default) -> 55.
    assert brands["Vintage Martini"]["sentiment_score"] == 85
    assert brands["Thrift Town"]["sentiment_score"] == 25
    assert brands["Plain Mention"]["sentiment_score"] == 55


@pytest.mark.django_db
def test_prompt_detail_visibility_varies_with_cross_model_overlap(auth):
    """A brand named by more models should outrank one named by fewer,
    and the denominator should be the number of answered responses."""
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best vintage shop in dallas")
    BrandPrompt.objects.create(website=website, prompt=p)

    # Two answered responses + one failed cell (must not dilute the denom).
    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        competitors_mentioned=[
            {"name": "Vintage Martini", "position": 1},
            {"name": "Buffalo Exchange", "position": 2},
        ],
    )
    LLMRankingResultFactory(
        audit__website=website, provider="gpt4",
        prompt=p.text, source_prompt=p, query_succeeded=True,
        competitors_mentioned=[
            {"name": "Vintage Martini", "position": 1},  # named by both
        ],
    )
    LLMRankingResultFactory(
        audit__website=website, provider="gemini",
        prompt=p.text, source_prompt=p, query_succeeded=False,
        response_text="", error_message="service_unavailable: gemini",
    )

    body = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    assert body["total_responses"] == 2  # failed cell excluded
    brands = {b["name"]: b for b in body["brands"]}
    # Named by 2 of 2 answered responses -> 100%; the other by 1 -> 50%.
    assert brands["Vintage Martini"]["visibility_pct"] == 100.0
    assert brands["Buffalo Exchange"]["visibility_pct"] == 50.0
    # Most-visible brand sorts first.
    assert body["brands"][0]["name"] == "Vintage Martini"
    # Named by both models vs. one.
    assert brands["Vintage Martini"]["model_count"] == 2
    assert brands["Buffalo Exchange"]["model_count"] == 1
    assert len(brands["Vintage Martini"]["models"]) == 2


@pytest.mark.django_db
def test_prompt_detail_brands_fall_back_to_response_text(auth):
    """When competitors_mentioned is empty but the response lists brands,
    the brand table is parsed from the response text - so even a single
    model response yields the brand list with ranks."""
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="pizza ordering app in dfw")
    BrandPrompt.objects.create(website=website, prompt=p)

    response = (
        "Here are some popular pizza ordering apps in DFW:\n"
        "1. **Domino's Pizza** - customize and track orders.\n"
        "2. **Pizza Hut** - Hut Rewards program.\n"
        "3. **Papa John's** - Papa Rewards.\n"
        "4. **Grubhub** - local pizza shops.\n"
    )
    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p,
        response_text=response, query_succeeded=True,
        is_mentioned=False, competitors_mentioned=[],  # extraction was empty
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    brands = {b["name"]: b for b in resp.json()["brands"]}
    assert "Domino's Pizza" in brands
    assert "Pizza Hut" in brands
    assert brands["Domino's Pizza"]["avg_position"] == 1
    assert brands["Pizza Hut"]["avg_position"] == 2


@pytest.mark.django_db
def test_prompt_detail_per_model_rank_buckets_and_unavailable(auth):
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="how do I fix a squeaky floorboard?")
    BrandPrompt.objects.create(website=website, prompt=p)

    for rank in (1, 2, 12):
        LLMRankingResultFactory(
            audit__website=website,
            provider="claude",
            prompt=p.text, source_prompt=p,
            query_succeeded=True, is_mentioned=True, mention_rank=rank,
        )
    LLMRankingResultFactory(
        audit__website=website,
        provider="gemini",
        prompt=p.text, source_prompt=p,
        response_text="",
        query_succeeded=False,
        error_message="service_unavailable: gemini provider not enabled",
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    by_model = {m["provider"]: m for m in resp.json()["by_model"]}
    assert by_model["claude"]["rank_buckets"] == {"1": 1, "2-3": 1, "4-10": 0, "11+": 1}
    assert by_model["claude"]["avg_rank"] == 5.0
    assert by_model["gemini"]["unavailable"] == 1
    assert by_model["gemini"]["responses"] == 0


@pytest.mark.django_db
def test_reextract_backfills_competitors_on_old_rows(auth, settings):
    from unittest.mock import patch

    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    # Pin the staleness predicate to "never extracted" only, so the test is
    # deterministic regardless of whether the dev shell exports a key.
    settings.ANTHROPIC_API_KEY = ""

    client, _user, website = auth
    p = Prompt.objects.create(text="pizza ordering app in dfw")
    BrandPrompt.objects.create(website=website, prompt=p)

    # Old crawler row: has text, never extracted (extraction_model="").
    row = LLMRankingResultFactory(
        audit__website=website, provider="gpt4",
        prompt=p.text, source_prompt=p,
        response_text="Top apps: Domino's, Pizza Hut, Slice.",
        query_succeeded=True, is_mentioned=False,
        competitors_mentioned=[], extraction_model="",
    )

    # Detail should flag it as needing backfill.
    detail = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    assert detail["unextracted_count"] == 1
    assert detail["brands"] == []

    extraction = {
        "is_mentioned": False, "mention_rank": None, "sentiment": "not_mentioned",
        "mention_context": "", "is_linked": False,
        "competitors_mentioned": [
            {"name": "Domino's", "position": 1, "linked": False},
            {"name": "Pizza Hut", "position": 2, "linked": False},
            {"name": "Slice", "position": 3, "linked": False},
        ],
        "primary_recommendation": "Domino's", "citations": [],
        "confidence_score": 95.0, "extraction_model": "haiku",
        "extraction_version": "1",
    }
    with patch(
        "apps.llm_ranking.services.extraction_service.HaikuExtractionService.extract",
        return_value=extraction,
    ):
        resp = client.post(
            f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/reextract/"
        )
    assert resp.status_code == 200, resp.content
    assert resp.json()["reextracted"] == 1

    row.refresh_from_db()
    assert {c["name"] for c in row.competitors_mentioned} == {"Domino's", "Pizza Hut", "Slice"}
    assert row.extraction_model == "haiku"

    # Detail now lists the brands and no longer flags backfill.
    detail2 = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    ).json()
    assert detail2["unextracted_count"] == 0
    brand_names = {b["name"] for b in detail2["brands"]}
    assert {"Domino's", "Pizza Hut", "Slice"}.issubset(brand_names)


@pytest.mark.django_db
def test_prompt_detail_recent_chats_label_failed_and_unavailable(auth):
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="pizza ordering app in dfw")
    BrandPrompt.objects.create(website=website, prompt=p)

    LLMRankingResultFactory(
        audit__website=website, provider="claude",
        prompt=p.text, source_prompt=p,
        response_text="Here are some pizza apps...", query_succeeded=True,
    )
    LLMRankingResultFactory(
        audit__website=website, provider="gemini",
        prompt=p.text, source_prompt=p,
        response_text="", query_succeeded=False,
        error_message="service_unavailable: gemini provider not enabled",
    )
    LLMRankingResultFactory(
        audit__website=website, provider="gpt4",
        prompt=p.text, source_prompt=p,
        response_text="", query_succeeded=False,
        error_message="upstream 500",
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    by_provider = {c["provider"]: c for c in resp.json()["recent_chats"]}
    assert by_provider["claude"]["status"] == "complete"
    assert by_provider["gemini"]["status"] == "unavailable"
    assert by_provider["gpt4"]["status"] == "failed"
    assert by_provider["gpt4"]["error"] == "upstream 500"


@pytest.mark.django_db
def test_prompt_detail_top_domains_expose_sample_chats(auth):
    from apps.citations.services.extraction_service import extract_for_result
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory
    from apps.prompt_library.models import Prompt

    client, _user, website = auth
    p = Prompt.objects.create(text="best budgeting apps in 2026")
    BrandPrompt.objects.create(website=website, prompt=p)

    r = LLMRankingResultFactory(
        audit__website=website, provider="perplexity",
        prompt=p.text, source_prompt=p,
        response_text="Apps include Acme.", is_mentioned=True, mention_rank=1,
        citations=[{"url": "https://reddit.com/r/personalfinance"}],
    )
    extract_for_result(str(r.id))

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    domains = resp.json()["top_domains"]
    reddit = next(d for d in domains if "reddit.com" in d["apex_domain"])
    assert str(r.public_id) in reddit["sample_result_ids"]


@pytest.mark.django_db
def test_create_prompts_multiline_with_tags_and_location(auth, monkeypatch):
    import apps.llm_ranking.tasks as lr_tasks
    monkeypatch.setattr(lr_tasks.run_llm_ranking_audit, "delay", lambda **k: None)

    client, _, website = auth
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {
            "text": "best budgeting apps\nalternatives to rocket money\n",
            "topic": "ALi",
            "location": "US",
            "tags": ["branded", "transactional"],
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["created_count"] == 2
    assert len(body["brand_prompt_ids"]) == 2

    bps = BrandPrompt.objects.filter(website=website).select_related("prompt__industry")
    assert bps.count() == 2
    for bp in bps:
        assert bp.tags == ["branded", "transactional"]
        assert bp.location == "US"
        assert bp.prompt.industry.name == "ALi"


@pytest.mark.django_db
def test_create_prompt_scan_failure_does_not_break_create(auth, monkeypatch):
    import apps.llm_ranking.tasks as lr_tasks

    def boom(**k):
        raise RuntimeError("queue down")
    monkeypatch.setattr(lr_tasks.run_llm_ranking_audit, "delay", boom)

    client, _, website = auth
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {"text": "a resilient prompt", "topic": "ALi"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["scan_audit_id"] is None
    assert BrandPrompt.objects.filter(website=website).count() == 1


@pytest.mark.django_db
def test_create_prompt_routes_region_from_location(auth, monkeypatch):
    import apps.llm_ranking.services.scan_dispatch as sd
    monkeypatch.setattr(sd, "dispatch_scan", lambda audit_id: "noop")

    from apps.llm_ranking.models import LLMRankingAudit

    client, _, website = auth
    resp = client.post(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/",
        {"text": "best UK current accounts", "topic": "ALi", "location": "GB"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    audit = LLMRankingAudit.objects.filter(website=website).latest("created_at")
    assert audit.region == "uk"      # GB -> uk region
    assert audit.location == "GB"


@pytest.mark.django_db
def test_saved_agg_tag_filter_and_all_tags(auth, monkeypatch):
    import apps.llm_ranking.services.scan_dispatch as sd
    monkeypatch.setattr(sd, "dispatch_scan", lambda audit_id: "noop")

    client, _, website = auth
    base = f"/api/v1/prompt-library/websites/{website.id}/prompts/"
    client.post(base, {"text": "branded prompt one", "topic": "ALi",
                       "tags": ["branded"]}, format="json")
    client.post(base, {"text": "txn prompt two", "topic": "ALi",
                       "tags": ["transactional"]}, format="json")

    agg = f"/api/v1/prompt-library/websites/{website.id}/saved-prompts/agg/"
    full = client.get(agg).json()
    tag_names = {t["name"]: t["count"] for t in full["all_tags"]}
    assert tag_names.get("branded") == 1
    assert tag_names.get("transactional") == 1

    filtered = client.get(agg, {"tag": "branded"}).json()
    assert filtered["total"] == 1
    assert all("branded" in r["tags"] for r in filtered["rows"])


@pytest.mark.django_db
def test_regions_endpoint(auth):
    client, _, _ = auth
    resp = client.get("/api/v1/prompt-library/regions/")
    assert resp.status_code == 200
    codes = {c["code"] for c in resp.json()["countries"]}
    assert {"US", "RU", "UA", "GB"} <= codes


@pytest.mark.django_db
def test_prompt_detail_by_model_visibility(auth, settings):
    from apps.llm_ranking.models import LLMRankingResult
    from apps.llm_ranking.tests.factories import LLMRankingResultFactory

    settings.PERPLEXITY_API_KEY = "configured-key"
    settings.XAI_API_KEY = ""  # Grok not configured

    client, user, website = auth
    industry = IndustryFactory(name="ALi", slug="ali")
    p = PromptFactory(industry=industry, text="best money apps")
    BrandPrompt.objects.create(website=website, prompt=p)
    audit = LLMRankingAuditFactory(website=website, created_by=user)
    LLMRankingResultFactory(
        audit=audit, provider=LLMRankingResult.PROVIDER_PERPLEXITY, prompt_index=0,
        prompt="best money apps", response_text="...", is_mentioned=True,
    )

    resp = client.get(
        f"/api/v1/prompt-library/websites/{website.id}/prompts/{p.id}/detail/"
    )
    assert resp.status_code == 200, resp.content
    by_model = {m["provider"]: m for m in resp.json()["by_model"]}
    # Every implemented provider is represented.
    assert {"claude", "gpt4", "gemini", "perplexity", "grok"} <= set(by_model)
    assert by_model["perplexity"]["configured"] is True
    assert by_model["perplexity"]["responses"] == 1
    assert by_model["perplexity"]["visibility_pct"] == 100.0
    # No key -> flagged not configured (so the UI shows "Not configured").
    assert by_model["grok"]["configured"] is False
    assert by_model["grok"]["responses"] == 0


@pytest.mark.django_db
def test_re_adding_archived_prompt_unarchives_it(auth):
    client, _, website = auth
    prompt = PromptFactory(text="Best payroll software for startups?")
    bp = BrandPrompt.objects.create(website=website, prompt=prompt, is_archived=True)

    with patch(
        "apps.prompt_library.api.v1.views.WebsitePromptCreateView._trigger_scan",
        return_value=None,
    ):
        resp = client.post(
            f"/api/v1/prompt-library/websites/{website.id}/prompts/",
            {"text": prompt.text},
            format="json",
        )

    assert resp.status_code == 201, resp.content
    bp.refresh_from_db()
    assert bp.is_archived is False
    assert BrandPrompt.objects.filter(website=website, prompt=prompt).count() == 1
