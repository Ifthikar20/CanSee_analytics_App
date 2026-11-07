"""Brand-vs-information classification in the chat detail payload.

Generic terms from an answer's numbered list ("Golf Clubs", "Golf Bag")
must render as information, never as competitor brands. Fully offline.
"""

import pytest

from apps.citations.models import Citation
from apps.citations.services.url_analytics import build_chat_detail
from apps.llm_ranking.models import LLMRankingAudit
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.websites.tests.factories import WebsiteFactory

RESPONSE = """If you're new to golf, here are the main tools:

1. **Golf Clubs** — your primary playing tools.
2. **Golf Balls** — the ball you hit on every shot.
3. **Titleist** — a trusted equipment maker.

Many beginners shop at golfscot.com for starter sets.
"""


@pytest.fixture
def detail_setup(db):
    website = WebsiteFactory(
        name="strix",
        competitors=[{"name": "Callaway", "domain": "callaway.com"}],
    )
    audit = LLMRankingAuditFactory(
        website=website, created_by=website.user,
        status=LLMRankingAudit.STATUS_COMPLETED,
    )
    result = LLMRankingResultFactory(
        audit=audit,
        response_text=RESPONSE,
        is_mentioned=False,
        mention_rank=None,
        competitors_mentioned=[
            # v4 entry: extractor already classified it.
            {"name": "Titleist", "position": 3, "linked": False,
             "sentiment": "positive", "domain": "titleist.com", "kind": "brand"},
            # v4 generic entry.
            {"name": "Golf Balls", "position": 2, "linked": False,
             "sentiment": "neutral", "domain": "", "kind": "generic"},
            # Pre-v4 entry with no kind and no domain, matches tracked
            # competitor list -> brand via fallback.
            {"name": "Callaway", "position": None, "linked": False,
             "sentiment": "neutral", "domain": ""},
            # Pre-v4 entry whose name matches a domain cited by this very
            # answer -> brand via the cited-domain fallback.
            {"name": "golfscot.com", "position": None, "linked": True,
             "sentiment": "neutral", "domain": ""},
        ],
    )
    Citation.objects.create(
        result=result, audit=audit,
        url="https://golfscot.com/golf-equipment-for-beginners/",
        normalized_url="golfscot.com/golf-equipment-for-beginners",
        apex_domain="golfscot.com",
        position=1,
    )
    return website, result


@pytest.mark.django_db
class TestBrandVsInfo:
    def test_classification(self, detail_setup):
        website, result = detail_setup
        detail = build_chat_detail(website, result_id=str(result.public_id))
        kinds = {b["name"]: b["kind"] for b in detail["brands"]}

        # Real brands, three different evidence paths:
        assert kinds["Titleist"] == "brand"          # extractor-tagged
        assert kinds["Callaway"] == "brand"          # tracked competitor
        assert kinds["golfscot.com"] == "brand"      # matches cited domain

        # Generic terms:
        assert kinds["Golf Balls"] == "info"         # extractor-tagged
        assert kinds["Golf Clubs"] == "info"         # regex list heading, no evidence

    def test_no_bare_regex_heading_promoted_to_brand(self, detail_setup):
        website, result = detail_setup
        detail = build_chat_detail(website, result_id=str(result.public_id))
        info_names = {b["name"] for b in detail["brands"] if b["kind"] == "info"}
        brand_names = {b["name"] for b in detail["brands"] if b["kind"] == "brand"}
        assert "Golf Clubs" in info_names
        assert "Golf Clubs" not in brand_names
