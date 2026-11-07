"""API tests for the AI Brand Safety endpoints."""
from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.brand_vault.models import SafetyAlert, SafetyPrompt
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _safety_settings(settings):
    settings.BRAND_VAULT_EXTRACTION_ENABLED = False
    settings.CLAIM_VERIFICATION_ENABLED = False
    settings.CITATION_EXTRACTION_ENABLED = False


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _make_alert(website, **kwargs):
    defaults = dict(
        website=website,
        model="ChatGPT",
        prompt_text="Is brand safe?",
        snippet="brand is not FDIC-insured",
        issue=SafetyAlert.ISSUE_HALLUCINATION,
        detail="Incorrect coverage claim.",
        severity=SafetyAlert.SEVERITY_HIGH,
    )
    defaults.update(kwargs)
    return SafetyAlert.objects.create(**defaults)



class TestSafetyPrompts:
    def test_requires_auth(self):
        website = WebsiteFactory()
        url = reverse("brand-vault-safety-prompts", args=[website.id])
        assert APIClient().get(url).status_code == 401

    def test_list_empty(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-safety-prompts", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert results == []

    def test_create_and_list(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-safety-prompts", args=[website.id])

        resp = client.post(url, {"text": "Is brand safe?"}, format="json")
        assert resp.status_code == 201
        assert resp.data["text"] == "Is brand safe?"
        assert resp.data["status"] == "active"
        assert resp.data["hits"] == 0

        resp = client.get(url)
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1

    def test_create_is_idempotent(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-safety-prompts", args=[website.id])

        client.post(url, {"text": "dup prompt"}, format="json")
        resp = client.post(url, {"text": "dup prompt"}, format="json")
        assert resp.status_code == 200
        assert SafetyPrompt.objects.filter(website=website).count() == 1

    def test_list_includes_hits_count(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        prompt = SafetyPrompt.objects.create(website=website, text="x")
        _make_alert(website, prompt=prompt)
        _make_alert(website, prompt=prompt, status=SafetyAlert.STATUS_DISMISSED)

        url = reverse("brand-vault-safety-prompts", args=[website.id])
        resp = client.get(url)
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert results[0]["hits"] == 1  # dismissed excluded

    def test_delete(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        prompt = SafetyPrompt.objects.create(website=website, text="kill me")
        url = reverse("brand-vault-safety-prompt-detail", args=[prompt.id])
        resp = client.delete(url)
        assert resp.status_code == 204
        assert not SafetyPrompt.objects.filter(id=prompt.id).exists()

    def test_delete_blocks_other_tenant(self, auth_client):
        client, _user = auth_client
        other_website = WebsiteFactory()
        prompt = SafetyPrompt.objects.create(website=other_website, text="x")
        url = reverse("brand-vault-safety-prompt-detail", args=[prompt.id])
        resp = client.delete(url)
        assert resp.status_code in (403, 404)
        assert SafetyPrompt.objects.filter(id=prompt.id).exists()



class TestSafetyAlerts:
    def test_list_filters_by_status_and_severity(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        _make_alert(website, severity=SafetyAlert.SEVERITY_HIGH)
        _make_alert(website, severity=SafetyAlert.SEVERITY_LOW,
                    status=SafetyAlert.STATUS_RESOLVED)
        url = reverse("brand-vault-safety-alerts", args=[website.id])

        resp = client.get(url, {"status": "open"})
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1
        assert results[0]["severity"] == "high"

        resp = client.get(url, {"severity": "low"})
        results = resp.data["results"] if "results" in resp.data else resp.data
        assert len(results) == 1
        assert results[0]["status"] == "resolved"

    def test_resolve(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        alert = _make_alert(website)
        url = reverse("brand-vault-safety-alert-resolve", args=[alert.id])
        resp = client.post(url, {"action": "resolve"}, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "resolved"
        alert.refresh_from_db()
        assert alert.resolved_by_id == user.id
        assert alert.resolved_at is not None

    def test_dismiss(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        alert = _make_alert(website)
        url = reverse("brand-vault-safety-alert-resolve", args=[alert.id])
        resp = client.post(url, {"action": "dismiss"}, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "dismissed"



class TestSafetyMetrics:
    def test_empty_state_returns_full_health(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        url = reverse("brand-vault-safety-metrics", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.data == {
            "reputation_health": 100,
            "hallucinations": 0,
            "harmful_mentions": 0,
            "prompts_monitored": 0,
            "open_alerts": 0,
        }

    def test_health_drops_with_open_alerts(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        _make_alert(website, severity=SafetyAlert.SEVERITY_HIGH,
                    issue=SafetyAlert.ISSUE_HALLUCINATION)
        _make_alert(website, severity=SafetyAlert.SEVERITY_MEDIUM,
                    issue=SafetyAlert.ISSUE_HARMFUL)
        # resolved alerts don't count
        _make_alert(website, severity=SafetyAlert.SEVERITY_HIGH,
                    status=SafetyAlert.STATUS_RESOLVED)
        SafetyPrompt.objects.create(website=website, text="p1")

        url = reverse("brand-vault-safety-metrics", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        # 12 (high) + 5 (medium) = 17 penalty -> health 83
        assert resp.data["reputation_health"] == 83
        assert resp.data["hallucinations"] == 1
        assert resp.data["harmful_mentions"] == 1
        assert resp.data["prompts_monitored"] == 1
        assert resp.data["open_alerts"] == 2
