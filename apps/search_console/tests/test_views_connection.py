"""Tests for GSC connection endpoints (connect, callback, property, disconnect)."""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.search_console.services import oauth_service
from apps.search_console.tests.factories import GscIntegrationFactory
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    settings.FRONTEND_URL = "http://frontend.test"


@pytest.fixture
def auth_client():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


@pytest.mark.django_db
class TestConnectStart:
    def test_returns_authorize_url(self, auth_client):
        client, user, website = auth_client
        response = client.post(f"/api/v1/search-console/{website.id}/connect/")
        assert response.status_code == 200
        url = response.json()["data"]["authorize_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "access_type=offline" in url

    def test_400_when_not_configured(self, auth_client, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
        client, user, website = auth_client
        response = client.post(f"/api/v1/search-console/{website.id}/connect/")
        assert response.status_code == 400

    def test_404_for_other_users_website(self, auth_client):
        client, user, website = auth_client
        other = WebsiteFactory()
        response = client.post(f"/api/v1/search-console/{other.id}/connect/")
        assert response.status_code == 404

    def test_401_unauthenticated(self, db):
        website = WebsiteFactory()
        response = APIClient().post(f"/api/v1/search-console/{website.id}/connect/")
        assert response.status_code == 401


@pytest.mark.django_db
class TestOAuthCallback:
    def _state_for(self, website):
        from django.core import signing

        return signing.dumps(
            {"website_id": str(website.id), "user_id": str(website.user_id)},
            salt=oauth_service.STATE_SALT,
        )

    def test_happy_path_connects_and_redirects(self, db):
        website = WebsiteFactory(url="https://example.com")
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        sites = [{"site_url": "sc-domain:example.com", "permission_level": "siteOwner"}]

        with patch.object(oauth_service, "exchange_code", return_value=tokens), \
             patch.object(oauth_service.gsc_client, "list_sites", return_value=sites), \
             patch("apps.search_console.tasks.sync_website_gsc.delay") as mock_sync:
            response = APIClient().get(
                "/api/v1/search-console/oauth/callback/",
                {"code": "c", "state": self._state_for(website)},
            )

        assert response.status_code == 302
        assert response["Location"] == (
            f"http://frontend.test/app/integrations?gsc=connected&website_id={website.id}"
        )
        integration = Integration.objects.get(website=website, type="gsc")
        assert integration.access_token == "at"
        assert integration.metadata["site_url"] == "sc-domain:example.com"
        mock_sync.assert_called_once()

    def test_no_property_match_redirects_to_selection(self, db):
        website = WebsiteFactory(url="https://example.com")
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        sites = [{"site_url": "sc-domain:unrelated.com", "permission_level": "siteOwner"}]

        with patch.object(oauth_service, "exchange_code", return_value=tokens), \
             patch.object(oauth_service.gsc_client, "list_sites", return_value=sites), \
             patch("apps.search_console.tasks.sync_website_gsc.delay") as mock_sync:
            response = APIClient().get(
                "/api/v1/search-console/oauth/callback/",
                {"code": "c", "state": self._state_for(website)},
            )

        assert response.status_code == 302
        assert "gsc=select_property" in response["Location"]
        mock_sync.assert_not_called()

    def test_invalid_state_redirects_with_error(self, db):
        response = APIClient().get(
            "/api/v1/search-console/oauth/callback/",
            {"code": "c", "state": "tampered"},
        )
        assert response.status_code == 302
        assert "gsc=error" in response["Location"]
        assert "reason=invalid_state" in response["Location"]

    def test_user_denied_redirects_with_error(self, db):
        website = WebsiteFactory()
        response = APIClient().get(
            "/api/v1/search-console/oauth/callback/",
            {"error": "access_denied", "state": self._state_for(website)},
        )
        assert response.status_code == 302
        assert "reason=denied" in response["Location"]

    def test_exchange_failure_redirects_with_error(self, db):
        import requests as requests_lib

        website = WebsiteFactory()
        with patch.object(
            oauth_service, "exchange_code",
            side_effect=requests_lib.RequestException("boom"),
        ):
            response = APIClient().get(
                "/api/v1/search-console/oauth/callback/",
                {"code": "c", "state": self._state_for(website)},
            )
        assert response.status_code == 302
        assert "reason=exchange_failed" in response["Location"]


@pytest.mark.django_db
class TestStatusPropertyDisconnect:
    def test_status_disconnected(self, auth_client):
        client, user, website = auth_client
        response = client.get(f"/api/v1/search-console/{website.id}/status/")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["connected"] is False
        assert data["has_data"] is False
        assert data["configured"] is True

    def test_status_connected(self, auth_client):
        client, user, website = auth_client
        GscIntegrationFactory(website=website)
        response = client.get(f"/api/v1/search-console/{website.id}/status/")
        data = response.json()["data"]
        assert data["connected"] is True
        assert data["is_active"] is True
        assert data["site_url"] == "sc-domain:example.com"

    def test_properties_409_when_not_connected(self, auth_client):
        client, user, website = auth_client
        response = client.get(f"/api/v1/search-console/{website.id}/properties/")
        assert response.status_code == 409

    def test_properties_returns_cached_sites(self, auth_client):
        client, user, website = auth_client
        sites = [{"site_url": "sc-domain:example.com", "permission_level": "siteOwner"}]
        GscIntegrationFactory(
            website=website,
            metadata={"available_sites": sites, "pending_property_selection": True},
        )
        response = client.get(f"/api/v1/search-console/{website.id}/properties/")
        assert response.json()["data"]["properties"] == sites

    def test_select_property_validates_and_triggers_backfill(self, auth_client):
        client, user, website = auth_client
        sites = [{"site_url": "sc-domain:example.com", "permission_level": "siteOwner"}]
        integration = GscIntegrationFactory(
            website=website,
            metadata={"available_sites": sites, "pending_property_selection": True},
        )
        with patch("apps.search_console.tasks.sync_website_gsc.delay") as mock_sync:
            response = client.post(
                f"/api/v1/search-console/{website.id}/property/",
                {"site_url": "sc-domain:example.com"},
                format="json",
            )
        assert response.status_code == 200
        integration.refresh_from_db()
        assert integration.metadata["site_url"] == "sc-domain:example.com"
        assert "pending_property_selection" not in integration.metadata
        mock_sync.assert_called_once()

    def test_select_property_rejects_unknown_site(self, auth_client):
        client, user, website = auth_client
        GscIntegrationFactory(
            website=website,
            metadata={"available_sites": [
                {"site_url": "sc-domain:example.com", "permission_level": "siteOwner"}
            ]},
        )
        response = client.post(
            f"/api/v1/search-console/{website.id}/property/",
            {"site_url": "sc-domain:evil.com"},
            format="json",
        )
        assert response.status_code == 400

    def test_disconnect(self, auth_client):
        client, user, website = auth_client
        integration = GscIntegrationFactory(website=website)
        with patch.object(oauth_service.requests, "post"):
            response = client.delete(f"/api/v1/search-console/{website.id}/connection/")
        assert response.status_code == 204
        integration.refresh_from_db()
        assert integration.is_active is False
        assert integration.access_token == ""

    def test_disconnect_without_integration_is_noop(self, auth_client):
        client, user, website = auth_client
        response = client.delete(f"/api/v1/search-console/{website.id}/connection/")
        assert response.status_code == 204
