"""Service-level tests for brand_vault."""
from __future__ import annotations

import pytest

from apps.brand_vault.models import BrandFact, FactRevision, FactStatus
from apps.brand_vault.services.fact_extractor import persist_extracted_items
from apps.brand_vault.services.fact_versioning import (
    approve_fact,
    reject_fact,
    supersede_fact,
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


class TestPersistExtractedItems:
    def test_drops_low_confidence(self):
        website = WebsiteFactory()
        items = [
            {"subject": "Acme", "predicate": "is", "object": "SaaS tool", "confidence": 0.2},
        ]
        out = persist_extracted_items(items, website=website)
        assert out == []
        assert BrandFact.objects.count() == 0

    def test_auto_approves_high_confidence(self):
        website = WebsiteFactory()
        items = [
            {"subject": "Acme", "predicate": "founded in", "object": "2019", "confidence": 0.95},
        ]
        out = persist_extracted_items(items, website=website)
        assert len(out) == 1
        assert out[0].status == FactStatus.AUTO

    def test_pending_for_mid_confidence(self):
        website = WebsiteFactory()
        items = [
            {"subject": "Acme", "predicate": "supports", "object": "SSO", "confidence": 0.6},
        ]
        out = persist_extracted_items(items, website=website)
        assert out[0].status == FactStatus.PENDING

    def test_dedupes_existing(self):
        website = WebsiteFactory()
        BrandFact.objects.create(
            website=website, subject="Acme", predicate="is", object="A SaaS",
            confidence=0.9, status=FactStatus.APPROVED,
        )
        items = [
            {"subject": "acme", "predicate": "IS", "object": "A SaaS", "confidence": 0.95},
        ]
        out = persist_extracted_items(items, website=website)
        assert out == []
        assert BrandFact.objects.count() == 1


class TestVersioning:
    def test_supersede_links_old_and_new(self):
        website = WebsiteFactory()
        old = BrandFact.objects.create(
            website=website, subject="Acme", predicate="prices at",
            object="$10/mo", status=FactStatus.APPROVED, confidence=0.9,
        )
        new = supersede_fact(str(old.id), "Acme", "prices at", "$15/mo")
        old.refresh_from_db()
        assert old.version_to is not None
        assert old.superseded_by_id == new.id
        assert new.status == FactStatus.APPROVED
        assert FactRevision.objects.filter(fact=old, action="superseded").exists()

    def test_approve_emits_revision(self):
        website = WebsiteFactory()
        fact = BrandFact.objects.create(
            website=website, subject="Acme", predicate="is", object="X",
            status=FactStatus.PENDING,
        )
        approve_fact(str(fact.id))
        fact.refresh_from_db()
        assert fact.status == FactStatus.APPROVED
        assert FactRevision.objects.filter(fact=fact, action="approved").exists()

    def test_reject_sets_version_to(self):
        website = WebsiteFactory()
        fact = BrandFact.objects.create(
            website=website, subject="Acme", predicate="is", object="X",
            status=FactStatus.PENDING,
        )
        reject_fact(str(fact.id))
        fact.refresh_from_db()
        assert fact.status == FactStatus.REJECTED
        assert fact.version_to is not None
