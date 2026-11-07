"""API tests for the content_studio endpoints."""
from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.content_studio.models import (
    BriefStatus,
    ContentBrief,
    ContentDraft,
    ContentFormat,
    DraftStatus,
    GapType,
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


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _seed_brief(user, **kwargs):
    website = WebsiteFactory(user=user)
    brief = ContentBrief.objects.create(
        website=website,
        gap_type=GapType.VISIBILITY.value,
        impact_score=kwargs.get("impact_score", 0.5),
        target_format=ContentFormat.BLOG.value,
        headline=kwargs.get("headline", "Brief headline"),
        description="why",
    )
    return website, brief


def test_briefs_list_requires_auth():
    website = WebsiteFactory()
    client = APIClient()
    url = reverse("content-studio-website-briefs", args=[website.id])
    resp = client.get(url)
    assert resp.status_code in (401, 403)


def test_briefs_list_tenant_isolation(auth_client):
    client, user = auth_client
    _seed_brief(user, headline="mine")
    other_user = UserFactory()
    _seed_brief(other_user, headline="not mine")

    website_mine = ContentBrief.objects.get(headline="mine").website
    url = reverse("content-studio-website-briefs", args=[website_mine.id])
    resp = client.get(url)
    assert resp.status_code == 200
    body = resp.json()
    items = body.get("results") if isinstance(body, dict) and "results" in body else body
    titles = [b["headline"] for b in items]
    assert "mine" in titles
    assert "not mine" not in titles

    other_website = ContentBrief.objects.get(headline="not mine").website
    resp2 = client.get(reverse("content-studio-website-briefs", args=[other_website.id]))
    assert resp2.status_code in (403, 404)


def test_briefs_list_filter_by_status(auth_client):
    client, user = auth_client
    website, b1 = _seed_brief(user, headline="open one")
    ContentBrief.objects.create(
        website=website, gap_type=GapType.VISIBILITY.value,
        target_format=ContentFormat.BLOG.value,
        headline="skipped one", description="",
        status=BriefStatus.SKIPPED.value,
    )
    url = reverse("content-studio-website-briefs", args=[website.id])
    resp = client.get(url, {"status": "skipped"})
    assert resp.status_code == 200
    body = resp.json()
    items = body.get("results") if isinstance(body, dict) and "results" in body else body
    headlines = [b["headline"] for b in items]
    assert "skipped one" in headlines
    assert "open one" not in headlines


def test_brief_detail_and_skip(auth_client):
    client, user = auth_client
    _website, brief = _seed_brief(user)
    detail_url = reverse("content-studio-brief-detail", args=[brief.id])
    resp = client.get(detail_url)
    assert resp.status_code == 200

    skip_url = reverse("content-studio-brief-skip", args=[brief.id])
    resp2 = client.post(skip_url)
    assert resp2.status_code == 200
    brief.refresh_from_db()
    assert brief.status == BriefStatus.SKIPPED.value


def test_draft_patch_marks_edited(auth_client):
    client, user = auth_client
    website, brief = _seed_brief(user)
    draft = ContentDraft.objects.create(
        brief=brief, website=website,
        title="t", body_markdown="x", status=DraftStatus.READY.value,
    )
    url = reverse("content-studio-draft-detail", args=[draft.id])
    resp = client.patch(url, {"body_markdown": "new body"}, format="json")
    assert resp.status_code == 200
    draft.refresh_from_db()
    assert draft.body_markdown == "new body"
    assert draft.status == DraftStatus.EDITED.value


def test_draft_approve(auth_client):
    client, user = auth_client
    website, brief = _seed_brief(user)
    draft = ContentDraft.objects.create(
        brief=brief, website=website, title="t", body_markdown="x",
        status=DraftStatus.READY.value,
    )
    url = reverse("content-studio-draft-approve", args=[draft.id])
    resp = client.post(url)
    assert resp.status_code == 200
    draft.refresh_from_db()
    assert draft.status == DraftStatus.APPROVED.value


