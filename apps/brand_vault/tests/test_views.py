"""API tests for the brand_vault endpoints."""
from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.brand_vault.models import BrandFact, FactStatus
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


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _seed_fact(user, **kwargs):
    website = WebsiteFactory(user=user)
    fact = BrandFact.objects.create(
        website=website, subject="Acme", predicate="is",
        object="A SaaS", status=FactStatus.PENDING, confidence=0.7,
        **kwargs,
    )
    return website, fact


class TestAuth:
    def test_facts_requires_auth(self):
        website = WebsiteFactory()
        url = reverse("brand-vault-website-facts", args=[website.id])
        resp = APIClient().get(url)
        assert resp.status_code == 401


class TestFactsList:
    def test_lists_facts_for_owner(self, auth_client):
        client, user = auth_client
        website, _ = _seed_fact(user)
        url = reverse("brand-vault-website-facts", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1

    def test_filter_by_status(self, auth_client):
        client, user = auth_client
        website, fact = _seed_fact(user)
        BrandFact.objects.create(
            website=website, subject="Acme", predicate="has", object="API",
            status=FactStatus.APPROVED, confidence=0.95,
        )
        url = reverse("brand-vault-website-facts", args=[website.id])
        resp = client.get(url, {"status": "approved"})
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1
        assert results[0]["status"] == "approved"

    def test_search_q(self, auth_client):
        client, user = auth_client
        website, _ = _seed_fact(user)
        BrandFact.objects.create(
            website=website, subject="Other", predicate="x", object="y",
            status=FactStatus.PENDING,
        )
        url = reverse("brand-vault-website-facts", args=[website.id])
        resp = client.get(url, {"q": "acme"})
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1

    def test_tenant_isolation(self, auth_client):
        client, _ = auth_client
        other = UserFactory()
        website, _ = _seed_fact(other)
        url = reverse("brand-vault-website-facts", args=[website.id])
        resp = client.get(url)
        assert resp.status_code in (403, 404)


class TestFactActions:
    def test_approve(self, auth_client):
        client, user = auth_client
        _, fact = _seed_fact(user)
        url = reverse("brand-vault-fact-approve", args=[fact.id])
        resp = client.post(url)
        assert resp.status_code == 200
        fact.refresh_from_db()
        assert fact.status == FactStatus.APPROVED

    def test_reject(self, auth_client):
        client, user = auth_client
        _, fact = _seed_fact(user)
        url = reverse("brand-vault-fact-reject", args=[fact.id])
        resp = client.post(url)
        assert resp.status_code == 200
        fact.refresh_from_db()
        assert fact.status == FactStatus.REJECTED

    def test_edit_supersedes(self, auth_client):
        client, user = auth_client
        _, fact = _seed_fact(user)
        url = reverse("brand-vault-fact-edit", args=[fact.id])
        resp = client.post(url, {
            "subject": "Acme", "predicate": "is", "object": "A new SaaS",
        }, format="json")
        assert resp.status_code == 201
        fact.refresh_from_db()
        assert fact.version_to is not None

    def test_stats(self, auth_client):
        client, user = auth_client
        website, _ = _seed_fact(user)
        url = reverse("brand-vault-website-stats", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.data["total"] >= 1
        assert "by_status" in resp.data


class TestFactImport:
    def test_import_json_creates_facts(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-website-facts-import", args=[website.id])
        payload = {"facts": [
            {"subject": "Acme", "predicate": "supports", "object": "Webhooks",
             "confidence": 0.95},
            {"subject": "Acme", "predicate": "offers", "object": "API access"},
        ]}
        resp = client.post(url, payload, format="json")
        assert resp.status_code == 200
        assert resp.data["created"] == 2
        assert resp.data["skipped"] == 0
        assert BrandFact.objects.filter(
            website=website, extracted_by="manual",
            status=FactStatus.APPROVED,
        ).count() == 2

    def test_import_json_is_idempotent(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        BrandFact.objects.create(
            website=website, subject="Acme", predicate="supports",
            object="Webhooks", status=FactStatus.APPROVED, confidence=0.9,
        )
        url = reverse("brand-vault-website-facts-import", args=[website.id])
        payload = {"facts": [
            {"subject": "Acme", "predicate": "supports", "object": "Webhooks"},
        ]}
        resp = client.post(url, payload, format="json")
        assert resp.status_code == 200
        assert resp.data["created"] == 0
        assert resp.data["skipped"] == 1

    def test_import_json_validates(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-website-facts-import", args=[website.id])
        payload = {"facts": [{"subject": "Acme"}]}
        resp = client.post(url, payload, format="json")
        assert resp.status_code == 200
        assert resp.data["created"] == 0
        assert resp.data["errors"]

    def test_import_csv_creates_facts(self, auth_client):
        from django.core.files.uploadedfile import SimpleUploadedFile
        client, user = auth_client
        website = WebsiteFactory(user=user)
        csv_body = (
            "subject,predicate,object,product_line,topic,confidence,source_url\n"
            "Acme,supports,Webhooks,api,integrations,0.9,\n"
            "Acme,offers,API,api,integrations,0.95,\n"
        )
        upload = SimpleUploadedFile("facts.csv", csv_body.encode("utf-8"),
                                    content_type="text/csv")
        url = reverse("brand-vault-website-facts-import-csv", args=[website.id])
        resp = client.post(url, {"file": upload}, format="multipart")
        assert resp.status_code == 200
        assert resp.data["created"] == 2
