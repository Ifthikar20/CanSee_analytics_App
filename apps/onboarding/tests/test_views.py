"""
Smoke tests for the onboarding API endpoints.

Exercises auth, SSRF rejection at the serializer edge, and the
happy-path orchestration. The orchestrator is mocked so we test the
view's own contract — payload shape and error handling — rather than
re-testing the service logic that ``test_onboarding_service.py`` covers.
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.onboarding.services.onboarding_service import OnboardingPayload


@pytest.fixture
def auth_client(db):
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.fixture
def no_api_keys(settings):
    settings.ANTHROPIC_API_KEY = ""
    settings.OPENAI_API_KEY = ""


@pytest.mark.django_db
class TestScanEndpoint:
    def test_requires_auth(self):
        client = APIClient()
        resp = client.post("/api/v1/onboarding/scan/", {"url": "https://example.com"}, format="json")
        assert resp.status_code in (401, 403)

    def test_rejects_unsafe_url(self, auth_client):
        client, _ = auth_client
        resp = client.post("/api/v1/onboarding/scan/",
                           {"url": "http://localhost/admin"}, format="json")
        # The global exception handler wraps serializer errors into the
        # {success, error} envelope; the 400 itself proves rejection.
        assert resp.status_code == 400
        assert resp.json()["success"] is False

    def test_rejects_missing_url(self, auth_client):
        client, _ = auth_client
        resp = client.post("/api/v1/onboarding/scan/", {}, format="json")
        assert resp.status_code == 400

    def test_returns_payload_on_success(self, auth_client, no_api_keys):
        client, _ = auth_client
        fake = OnboardingPayload(
            url="https://acme.com",
            success=True,
            business_name="Acme",
            industry="SaaS",
            description_short="Acme does X.",
            competitors=[{"name": "Mixpanel", "domain": "mixpanel.com"}],
        )
        with patch("apps.onboarding.api.v1.views.run_onboarding_scan",
                   return_value=fake):
            resp = client.post("/api/v1/onboarding/scan/",
                               {"url": "https://acme.com"}, format="json")
        assert resp.status_code == 200
        # EnvelopeRenderer wraps every success body as {success, data}.
        body = resp.json()["data"]
        assert body["success"] is True
        assert body["business_name"] == "Acme"
        assert len(body["competitors"]) == 1


@pytest.mark.django_db
class TestSaveEndpoint:
    def test_requires_auth(self):
        client = APIClient()
        resp = client.post("/api/v1/onboarding/save/", {}, format="json")
        assert resp.status_code in (401, 403)

    def test_rejects_unsafe_url(self, auth_client):
        client, _ = auth_client
        resp = client.post("/api/v1/onboarding/save/", {
            "url": "http://127.0.0.1/admin",
            "business_name": "X",
        }, format="json")
        assert resp.status_code == 400

    def test_creates_website(self, auth_client):
        client, user = auth_client
        # WebsiteService is imported inside the view (import cycle), so
        # it must be patched at its source module.
        with patch(
            "apps.websites.services.website_service.WebsiteService"
        ) as ws:
            class _Fake:
                id = "00000000-0000-0000-0000-000000000001"
                name = "Acme"
                url = "https://acme.com"
                description = ""
                topics = []
                def save(self): pass
            ws.create.return_value = _Fake()
            with patch("core.validators.url_safety.socket.getaddrinfo",
                       return_value=[(2, 1, 0, "", ("93.184.216.34", 0))]):
                resp = client.post("/api/v1/onboarding/save/", {
                    "url": "https://acme.com",
                    "business_name": "Acme",
                    "industry": "SaaS",
                    "description": "We do X.",
                    "keywords": ["funnels", "retention"],
                }, format="json")
        assert resp.status_code == 201
        # EnvelopeRenderer wraps every success body as {success, data}.
        body = resp.json()["data"]
        assert body["name"] == "Acme"
        ws.create.assert_called_once()

    def test_enforces_project_limit(self, auth_client, settings):
        """Onboarding /save/ must not bypass the plan's project cap."""
        from apps.websites.tests.factories import WebsiteFactory

        settings.PAYWALL_ENABLED = True
        client, user = auth_client
        WebsiteFactory(user=user, url="https://existing.example.com")
        with patch("core.validators.url_safety.socket.getaddrinfo",
                   return_value=[(2, 1, 0, "", ("93.184.216.34", 0))]):
            resp = client.post("/api/v1/onboarding/save/", {
                "url": "https://second.example.com",
                "business_name": "Second",
            }, format="json")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "project_limit_reached"

    def test_limit_does_not_block_updating_existing_site(self, auth_client, settings):
        """Re-running onboarding on the SAME url is an update, not a create."""
        from apps.websites.tests.factories import WebsiteFactory

        settings.PAYWALL_ENABLED = True
        client, user = auth_client
        WebsiteFactory(user=user, url="https://existing.example.com")
        with patch("core.validators.url_safety.socket.getaddrinfo",
                   return_value=[(2, 1, 0, "", ("93.184.216.34", 0))]):
            resp = client.post("/api/v1/onboarding/save/", {
                "url": "https://existing.example.com",
                "business_name": "Renamed",
            }, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["name"] == "Renamed"
