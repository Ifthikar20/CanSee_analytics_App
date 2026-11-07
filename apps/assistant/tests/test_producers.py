"""Phase 2 producer adapters.

Embedding is patched at the rag embedder so nothing hits the network;
we assert the DOCUMENTS land with the right tenant + provenance.
"""
from unittest.mock import patch

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.assistant.services import producers
from apps.brand_vault.models import SafetyAlert
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.websites.tests.factories import WebsiteFactory

# Deterministic stand-in for the embedding call inside ingest.
EMBED = "apps.rag.services.embedder.embed_texts"


def _fake_embed(texts, **kwargs):
    return [[0.1, 0.2, 0.3] for _ in texts], "test-model", 3


@pytest.fixture
def site(db):
    user = UserFactory()
    return WebsiteFactory(user=user)


@pytest.mark.django_db
class TestSavedPromptsProducer:
    def test_writes_scoped_source(self, site):
        BrandPrompt.objects.create(website=site, prompt=PromptFactory())
        with patch(EMBED, side_effect=_fake_embed):
            written = producers.ingest_saved_prompts(site.id)
        assert written == 1
        src = KnowledgeSource.objects.get(website=site, source_app="prompt_notes")
        assert src.user_id == site.user_id
        assert src.url == f"promptnote://{site.id}/saved"
        # Chunks carry the same tenant identity.
        assert KnowledgeChunk.objects.filter(source=src).exclude(
            user_id=site.user_id).count() == 0

    def test_idempotent(self, site):
        BrandPrompt.objects.create(website=site, prompt=PromptFactory())
        with patch(EMBED, side_effect=_fake_embed):
            producers.ingest_saved_prompts(site.id)
            producers.ingest_saved_prompts(site.id)
        assert KnowledgeSource.objects.filter(
            website=site, source_app="prompt_notes").count() == 1

    def test_no_prompts_writes_nothing(self, site):
        with patch(EMBED, side_effect=_fake_embed):
            assert producers.ingest_saved_prompts(site.id) == 0

    def test_disabled_flag_writes_nothing(self, site, settings):
        settings.ASSISTANT_KNOWLEDGE_INGEST_ENABLED = False
        BrandPrompt.objects.create(website=site, prompt=PromptFactory())
        with patch(EMBED, side_effect=_fake_embed):
            assert producers.ingest_saved_prompts(site.id) == 0
        assert KnowledgeSource.objects.filter(website=site).count() == 0


@pytest.mark.django_db
class TestSecurityAlertProducer:
    def test_writes_open_alert(self, site):
        SafetyAlert.objects.create(
            website=site, snippet="Claims we offer a free tier that does not exist.",
            issue=SafetyAlert.ISSUE_HALLUCINATION,
            severity=SafetyAlert.SEVERITY_HIGH,
            status=SafetyAlert.STATUS_OPEN,
            model="claude", prompt_text="Does Cansee have a free tier?",
            detail="No free tier is offered.",
        )
        with patch(EMBED, side_effect=_fake_embed):
            written = producers.ingest_security_alerts(site.id)
        assert written == 1
        src = KnowledgeSource.objects.get(website=site, source_app="security_alert")
        assert src.user_id == site.user_id
        chunk = KnowledgeChunk.objects.filter(source=src).first()
        assert "free tier" in chunk.text


@pytest.mark.django_db
class TestTenantIsolationOfProducers:
    def test_documents_never_cross_tenants(self):
        a = WebsiteFactory(user=UserFactory())
        b = WebsiteFactory(user=UserFactory())
        BrandPrompt.objects.create(website=a, prompt=PromptFactory())
        BrandPrompt.objects.create(website=b, prompt=PromptFactory())
        with patch(EMBED, side_effect=_fake_embed):
            producers.ingest_saved_prompts(a.id)
            producers.ingest_saved_prompts(b.id)
        # Every chunk belongs to the owner of its own website.
        for src in KnowledgeSource.objects.all():
            assert src.user_id == src.website.user_id
            assert not KnowledgeChunk.objects.filter(source=src).exclude(
                user_id=src.website.user_id).exists()
        # And B's corpus never contains A's source.
        assert not KnowledgeSource.objects.filter(
            website=b, user_id=a.user_id).exists()
