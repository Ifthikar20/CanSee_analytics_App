"""Service-level tests for the content_studio app."""
from __future__ import annotations

import pytest

from apps.brand_vault.models import BrandFact, FactStatus, ToneSample
from apps.content_studio.models import (
    BriefStatus,
    ContentBrief,
    ContentDraft,
    ContentFormat,
    DraftStatus,
    GapType,
)
from apps.content_studio.services import (
    accuracy_guard,
    brief_generator,
    drafter,
    voice_guard,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet_pipelines(settings):
    # pytest 8 rejects non-Mark objects in pytestmark, so the old
    # module-level override_settings entry broke collection; the
    # pytest-django settings fixture is the supported equivalent.
    settings.BRAND_VAULT_EXTRACTION_ENABLED = False
    settings.CLAIM_VERIFICATION_ENABLED = False
    settings.CITATION_EXTRACTION_ENABLED = False
    settings.CONTENT_STUDIO_BRIEF_GENERATION_ENABLED = False


def _approved_fact(website, subject="Acme", obj="ships globally"):
    return BrandFact.objects.create(
        website=website, subject=subject, predicate="does",
        object=obj, status=FactStatus.APPROVED.value,
    )


def test_visibility_brief_created_when_mention_rate_low():
    website = WebsiteFactory()
    audit = LLMRankingAuditFactory(website=website)
    LLMRankingResultFactory(audit=audit, prompt="best CRM?", is_mentioned=False)
    LLMRankingResultFactory(audit=audit, prompt="best CRM?", is_mentioned=False)
    LLMRankingResultFactory(audit=audit, prompt="best CRM?", is_mentioned=False)

    n = brief_generator.generate_briefs_for_website(str(website.id))
    assert n >= 1
    brief = ContentBrief.objects.filter(website=website, gap_type=GapType.VISIBILITY.value).first()
    assert brief is not None
    assert brief.target_format == ContentFormat.BLOG.value


def test_briefs_ordered_by_impact_desc():
    website = WebsiteFactory()
    ContentBrief.objects.create(
        website=website, gap_type=GapType.VISIBILITY.value, impact_score=0.4,
        target_format=ContentFormat.BLOG.value, headline="low", description="",
    )
    ContentBrief.objects.create(
        website=website, gap_type=GapType.VISIBILITY.value, impact_score=0.9,
        target_format=ContentFormat.BLOG.value, headline="high", description="",
    )
    first = ContentBrief.objects.filter(website=website).first()
    assert first.headline == "high"


def test_drafter_persists_draft_and_runs_guards(monkeypatch):
    monkeypatch.setattr(drafter, "_call_anthropic", lambda s, u, **kw: "")
    website = WebsiteFactory()
    _approved_fact(website)
    brief = ContentBrief.objects.create(
        website=website, gap_type=GapType.VISIBILITY.value, impact_score=0.5,
        target_format=ContentFormat.BLOG.value, headline="Test brief",
        description="why",
        suggested_structure={"sections": [{"heading": "Intro", "summary": "x"}]},
    )
    draft = drafter.draft_content(str(brief.id))
    assert draft.status == DraftStatus.READY.value
    assert draft.body_markdown
    assert draft.voice_score is not None
    assert draft.accuracy_score is not None
    brief.refresh_from_db()
    assert brief.status == BriefStatus.DRAFTED.value


def test_drafter_regenerate_bumps_revision(monkeypatch):
    monkeypatch.setattr(drafter, "_call_anthropic", lambda s, u, **kw: "")
    website = WebsiteFactory()
    brief = ContentBrief.objects.create(
        website=website, gap_type=GapType.VISIBILITY.value, impact_score=0.5,
        target_format=ContentFormat.BLOG.value, headline="X", description="",
    )
    d1 = drafter.draft_content(str(brief.id))
    d2 = drafter.draft_content(str(brief.id), regenerate=True)
    assert d1.revision == 1
    assert d2.revision == 2


def test_voice_guard_no_samples():
    website = WebsiteFactory()
    draft = ContentDraft.objects.create(
        brief=ContentBrief.objects.create(
            website=website, gap_type=GapType.VISIBILITY.value,
            target_format=ContentFormat.BLOG.value,
            headline="x", description="",
        ),
        website=website, title="t", body_markdown="hello world",
    )
    score, notes = voice_guard.score_voice(draft)
    assert score == 0.5
    assert "no tone samples" in notes


def test_voice_guard_with_samples():
    website = WebsiteFactory()
    ToneSample.objects.create(
        website=website, text="We build helpful products for growing teams.",
        text_hash="h1", word_count=8,
    )
    draft = ContentDraft.objects.create(
        brief=ContentBrief.objects.create(
            website=website, gap_type=GapType.VISIBILITY.value,
            target_format=ContentFormat.BLOG.value,
            headline="x", description="",
        ),
        website=website, title="t",
        body_markdown="We build helpful products for growing teams.",
    )
    score, notes = voice_guard.score_voice(draft)
    assert 0.0 <= score <= 1.0


def test_accuracy_guard_detects_unsupported_claim():
    website = WebsiteFactory()
    BrandFact.objects.create(
        website=website, subject="Acme", predicate="ships",
        object="globally to 50 countries", status=FactStatus.APPROVED.value,
    )
    draft = ContentDraft.objects.create(
        brief=ContentBrief.objects.create(
            website=website, gap_type=GapType.VISIBILITY.value,
            target_format=ContentFormat.BLOG.value,
            headline="x", description="",
        ),
        website=website, title="t",
        body_markdown=(
            "Acme ships globally to 50 countries every week. "
            "The company was founded on planet Mars in 1812."
        ),
    )
    score, notes = accuracy_guard.score_accuracy(draft)
    assert 0.0 <= score <= 1.0
    assert "claims" in notes.lower() or "verified" in notes.lower()

