"""Tests for the GSC-to-Prompt-Library feed."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.prompt_library.models import Industry, Prompt, PromptSource
from apps.prompt_library.services._hash import text_hash
from apps.search_console.services import prompt_feed
from apps.search_console.tests.factories import GscQueryStatFactory
from apps.websites.tests.factories import WebsiteFactory

RECENT = timezone.now().date() - timedelta(days=5)


@pytest.fixture
def industry(db):
    return Industry.objects.create(slug="saas", name="SaaS")


@pytest.fixture
def website(industry):
    return WebsiteFactory(
        name="Acme Analytics", url="https://acmeanalytics.io", industry="saas"
    )


@pytest.mark.django_db
def test_feed_creates_gsc_aggregate_prompts(website, settings):
    settings.GSC_PROMPT_MIN_IMPRESSIONS = 10
    GscQueryStatFactory(
        website=website, date=RECENT,
        query="best analytics tool for startups", impressions=500, clicks=20,
    )

    result = prompt_feed.feed_prompts_for_website(website)

    assert result["created"] == 1
    prompt = Prompt.objects.get(source=PromptSource.GSC_AGGREGATE)
    assert prompt.text == "best analytics tool for startups"
    assert prompt.industry.slug == "saas"
    assert prompt.text_hash == text_hash("best analytics tool for startups")
    assert prompt.demand_score > 0.1


@pytest.mark.django_db
def test_feed_skips_short_and_branded_queries(website, settings):
    settings.GSC_PROMPT_MIN_IMPRESSIONS = 10
    GscQueryStatFactory(website=website, date=RECENT, query="analytics", impressions=500)
    GscQueryStatFactory(
        website=website, date=RECENT, query="acme analytics pricing plans", impressions=500
    )
    GscQueryStatFactory(
        website=website, date=RECENT, query="acmeanalytics login page help", impressions=500
    )

    result = prompt_feed.feed_prompts_for_website(website)

    assert result["created"] == 0
    assert result["skipped"] == 3
    assert Prompt.objects.count() == 0


@pytest.mark.django_db
def test_feed_respects_min_impressions(website, settings):
    settings.GSC_PROMPT_MIN_IMPRESSIONS = 100
    GscQueryStatFactory(
        website=website, date=RECENT,
        query="how to track website visitors", impressions=50,
    )
    result = prompt_feed.feed_prompts_for_website(website)
    assert result["created"] == 0
    assert Prompt.objects.count() == 0


@pytest.mark.django_db
def test_feed_dedups_and_only_bumps_demand_upward(website, industry, settings):
    settings.GSC_PROMPT_MIN_IMPRESSIONS = 10
    text = "how to improve seo rankings fast"
    existing = Prompt.objects.create(
        industry=industry,
        text=text,
        intent_bucket="category",
        source=PromptSource.REDDIT,
        text_hash=text_hash(text),
        demand_score=0.9,
    )
    GscQueryStatFactory(website=website, date=RECENT, query=text, impressions=200)

    result = prompt_feed.feed_prompts_for_website(website)

    assert result["created"] == 0
    assert result["boosted"] == 0  # 200 impressions maps below 0.9
    existing.refresh_from_db()
    assert existing.demand_score == 0.9
    assert existing.source == PromptSource.REDDIT  # untouched

    # A much higher-demand signal bumps the score upward.
    GscQueryStatFactory(
        website=website, date=RECENT - timedelta(days=1), query=text, impressions=100000
    )
    result = prompt_feed.feed_prompts_for_website(website)
    assert result["boosted"] == 1
    existing.refresh_from_db()
    assert existing.demand_score > 0.9


@pytest.mark.django_db
def test_feed_skips_when_no_industry():
    website = WebsiteFactory(industry="")
    result = prompt_feed.feed_prompts_for_website(website)
    assert result == {"skipped": "no_industry"}
