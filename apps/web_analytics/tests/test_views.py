"""Endpoint tests: tenant isolation, feature gate, snapshot serving."""

from unittest.mock import patch

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.web_analytics.api.v1 import views
from apps.web_analytics.services import ga4_oauth
from apps.web_analytics.tests.factories import (
    CloudflareIntegrationFactory,
    Ga4IntegrationFactory,
)
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    settings.FRONTEND_URL = "http://frontend.test"
    settings.WEB_ANALYTICS_ENABLED = True
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def auth_client():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


@pytest.mark.django_db
class TestFeatureGateAndTenancy:
    def test_kill_switch_hides_every_endpoint(self, auth_client, settings):
        settings.WEB_ANALYTICS_ENABLED = False
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        assert client.get(f"/api/v1/web-analytics/ga4/{website.id}/status/").status_code == 404
        assert client.get(f"/api/v1/web-analytics/ga4/{website.id}/realtime/").status_code == 404
        assert client.get(f"/api/v1/web-analytics/cloudflare/{website.id}/status/").status_code == 404

    def test_404_for_other_users_website(self, auth_client):
        client, user, website = auth_client
        other = WebsiteFactory()
        Ga4IntegrationFactory(website=other)
        assert client.get(f"/api/v1/web-analytics/ga4/{other.id}/status/").status_code == 404
        assert client.get(f"/api/v1/web-analytics/ga4/{other.id}/realtime/").status_code == 404
        assert client.post(f"/api/v1/web-analytics/ga4/{other.id}/connect/").status_code == 404

    def test_401_unauthenticated(self, db):
        website = WebsiteFactory()
        resp = APIClient().get(f"/api/v1/web-analytics/ga4/{website.id}/status/")
        assert resp.status_code == 401


@pytest.mark.django_db
class TestGa4Connection:
    def test_connect_returns_authorize_url(self, auth_client):
        client, user, website = auth_client
        resp = client.post(f"/api/v1/web-analytics/ga4/{website.id}/connect/")
        assert resp.status_code == 200
        url = resp.json()["data"]["authorize_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "analytics.readonly" in url

    def test_connect_400_when_not_configured(self, auth_client, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
        client, user, website = auth_client
        resp = client.post(f"/api/v1/web-analytics/ga4/{website.id}/connect/")
        assert resp.status_code == 400

    def test_status_disconnected(self, auth_client):
        client, user, website = auth_client
        resp = client.get(f"/api/v1/web-analytics/ga4/{website.id}/status/")
        data = resp.json()["data"]
        assert data["connected"] is False
        assert data["configured"] is True
        assert data["feature_enabled"] is True

    def test_status_connected(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        data = client.get(f"/api/v1/web-analytics/ga4/{website.id}/status/").json()["data"]
        assert data["is_active"] is True
        assert data["property_id"] == "123456"

    def test_callback_connects_and_redirects_to_website_page(self, db):
        website = WebsiteFactory()
        state = ga4_oauth.build_authorize_url(website=website, user=website.user)
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(state).query)["state"][0]
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        props = [{"property_id": "111", "display_name": "Only", "account_name": "A"}]

        with patch.object(ga4_oauth, "exchange_code", return_value=tokens), \
             patch.object(ga4_oauth.ga4_client, "list_account_summaries", return_value=props):
            resp = APIClient().get(
                "/api/v1/web-analytics/ga4/oauth/callback/",
                {"code": "c", "state": state},
            )

        assert resp.status_code == 302
        assert resp["Location"] == f"http://frontend.test/websites/{website.id}?ga4=connected"
        integration = Integration.objects.get(website=website, type="ga")
        assert integration.metadata["property_id"] == "111"

    def test_callback_invalid_state_redirects_with_error(self, db):
        resp = APIClient().get(
            "/api/v1/web-analytics/ga4/oauth/callback/",
            {"code": "c", "state": "tampered"},
        )
        assert resp.status_code == 302
        assert resp["Location"].startswith("http://frontend.test/websites?ga4=error")

    def test_select_property_validates(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(
            website=website,
            metadata={"available_properties": [
                {"property_id": "111", "display_name": "One", "account_name": "A"}
            ], "pending_property_selection": True},
        )
        bad = client.post(
            f"/api/v1/web-analytics/ga4/{website.id}/property/",
            {"property_id": "999"}, format="json",
        )
        assert bad.status_code == 400
        ok = client.post(
            f"/api/v1/web-analytics/ga4/{website.id}/property/",
            {"property_id": "111"}, format="json",
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["property_id"] == "111"

    def test_disconnect_skips_revoke_while_gsc_active(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        Integration.objects.create(
            website=website, type="gsc", access_token="g", is_active=True
        )
        with patch.object(ga4_oauth.requests, "post") as mock_post:
            resp = client.delete(f"/api/v1/web-analytics/ga4/{website.id}/connection/")
        assert resp.status_code == 204
        mock_post.assert_not_called()


@pytest.mark.django_db
class TestGa4Realtime:
    def test_409_when_not_connected(self, auth_client):
        client, user, website = auth_client
        resp = client.get(f"/api/v1/web-analytics/ga4/{website.id}/realtime/")
        assert resp.status_code == 409

    def test_served_from_cache_without_upstream_call(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        cache.set(f"wa:ga4:{website.id}", {"active_users": 4, "stale": False}, 25)

        with patch.object(views.ga4_client, "build_realtime_snapshot") as mock_build:
            resp = client.get(f"/api/v1/web-analytics/ga4/{website.id}/realtime/")

        mock_build.assert_not_called()
        assert resp.json()["data"] == {"active_users": 4, "stale": False}

    def test_stale_fallback_on_upstream_failure(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        cache.set(f"wa:ga4:{website.id}:last", {"active_users": 2, "stale": True}, 600)

        with patch.object(views.ga4_client, "build_realtime_snapshot", return_value=None):
            resp = client.get(f"/api/v1/web-analytics/ga4/{website.id}/realtime/")

        data = resp.json()["data"]
        assert data["active_users"] == 2
        assert data["stale"] is True

    def test_fetches_and_caches_on_miss(self, auth_client):
        client, user, website = auth_client
        Ga4IntegrationFactory(website=website)
        snap = {"active_users": 9, "source": "ga4"}

        with patch.object(views.ga4_client, "build_realtime_snapshot", return_value=snap):
            resp = client.get(f"/api/v1/web-analytics/ga4/{website.id}/realtime/")

        assert resp.json()["data"]["active_users"] == 9
        assert cache.get(f"wa:ga4:{website.id}")["active_users"] == 9


@pytest.mark.django_db
class TestCloudflare:
    def test_connect_auto_matches_zone_and_hides_token(self, auth_client):
        client, user, website = auth_client
        zones = [
            {"id": "z1", "name": "example.com", "status": "active", "paused": False},
            {"id": "z2", "name": "other.io", "status": "active", "paused": False},
        ]
        website.url = "https://www.example.com/"
        website.save(update_fields=["url"])

        with patch.object(views.cloudflare_client, "verify_token", return_value=True), \
             patch.object(views.cloudflare_client, "list_zones", return_value=zones):
            resp = client.post(
                f"/api/v1/web-analytics/cloudflare/{website.id}/connect/",
                {"api_token": "super-secret-token"}, format="json",
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["zone_id"] == "z1"
        assert data["zone_name"] == "example.com"
        assert "super-secret-token" not in resp.content.decode()

        integration = Integration.objects.get(website=website, type="cloudflare")
        assert integration.access_token == "super-secret-token"

    def test_connect_pending_when_no_zone_matches(self, auth_client):
        client, user, website = auth_client
        zones = [{"id": "z9", "name": "unrelated.net", "status": "active", "paused": False}]
        with patch.object(views.cloudflare_client, "verify_token", return_value=True), \
             patch.object(views.cloudflare_client, "list_zones", return_value=zones):
            resp = client.post(
                f"/api/v1/web-analytics/cloudflare/{website.id}/connect/",
                {"api_token": "t"}, format="json",
            )
        data = resp.json()["data"]
        assert data["pending_zone_selection"] is True
        assert data["zone_id"] is None

    def test_connect_rejects_invalid_token(self, auth_client):
        client, user, website = auth_client
        with patch.object(views.cloudflare_client, "verify_token", return_value=False):
            resp = client.post(
                f"/api/v1/web-analytics/cloudflare/{website.id}/connect/",
                {"api_token": "bad"}, format="json",
            )
        assert resp.status_code == 400

    def test_snapshot_409_when_not_connected(self, auth_client):
        client, user, website = auth_client
        resp = client.get(f"/api/v1/web-analytics/cloudflare/{website.id}/snapshot/")
        assert resp.status_code == 409

    def test_snapshot_reads_through_cache(self, auth_client):
        client, user, website = auth_client
        CloudflareIntegrationFactory(website=website)
        snap = {"source": "cloudflare", "totals_24h": {"requests": 10, "visits": 5, "bytes": 1}}

        with patch.object(views.cloudflare_client, "build_zone_snapshot", return_value=dict(snap)) as mock_build:
            first = client.get(f"/api/v1/web-analytics/cloudflare/{website.id}/snapshot/")
            second = client.get(f"/api/v1/web-analytics/cloudflare/{website.id}/snapshot/")

        assert first.json()["data"]["totals_24h"]["requests"] == 10
        assert second.json()["data"]["zone_name"] == "example.com"
        mock_build.assert_called_once()

    def test_status_never_contains_token(self, auth_client):
        client, user, website = auth_client
        CloudflareIntegrationFactory(website=website)
        resp = client.get(f"/api/v1/web-analytics/cloudflare/{website.id}/status/")
        assert "cf-token" not in resp.content.decode()
        assert resp.json()["data"]["connected"] is True

    def test_disconnect_clears_token(self, auth_client):
        client, user, website = auth_client
        integration = CloudflareIntegrationFactory(website=website)
        resp = client.delete(f"/api/v1/web-analytics/cloudflare/{website.id}/connection/")
        assert resp.status_code == 204
        # RFC 9110: 204 must not carry a body — the EnvelopeRenderer must
        # not wrap it (a body here breaks proxies with a length mismatch).
        assert resp.content == b""
        integration.refresh_from_db()
        assert integration.access_token == ""
        assert integration.is_active is False


@pytest.mark.django_db
class TestHosted:
    def test_status_unconfigured(self, auth_client, settings):
        # Explicit: a developer .env may carry hosted-tag fixtures.
        settings.GA4_SA_CREDENTIALS_JSON = ""
        settings.GA4_HOSTED_PROPERTY_ID = ""
        client, user, website = auth_client
        data = client.get(f"/api/v1/web-analytics/ga4/hosted/{website.id}/status/").json()["data"]
        assert data["enabled"] is False
        assert data["configured"] is False

    def test_enable_400_when_not_configured(self, auth_client, settings):
        settings.GA4_SA_CREDENTIALS_JSON = ""
        settings.GA4_HOSTED_PROPERTY_ID = ""
        client, user, website = auth_client
        resp = client.post(f"/api/v1/web-analytics/ga4/hosted/{website.id}/enable/")
        assert resp.status_code == 400

    def test_enable_provisions_and_returns_snippet(self, auth_client, settings):
        settings.GA4_SA_CREDENTIALS_JSON = '{"client_email": "sa@x", "private_key": "k"}'
        settings.GA4_HOSTED_PROPERTY_ID = "555555"
        client, user, website = auth_client

        def fake_provision(site):
            integration, _ = Integration.objects.get_or_create(website=site, type="ga_hosted")
            integration.metadata = {"measurement_id": "G-XYZ", "stream_id": "1", "property_id": "555555"}
            integration.is_active = True
            integration.save()
            return integration, None

        with patch.object(views.ga4_hosted, "provision_stream", side_effect=fake_provision):
            resp = client.post(f"/api/v1/web-analytics/ga4/hosted/{website.id}/enable/")

        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["measurement_id"] == "G-XYZ"
        assert "googletagmanager.com/gtag/js?id=G-XYZ" in data["snippet"]
