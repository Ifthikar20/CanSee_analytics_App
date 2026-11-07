"""Unit tests for the GSC OAuth service."""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import requests as requests_lib
from django.core import signing

from apps.search_console.services import oauth_service
from apps.search_console.tests.factories import GscIntegrationFactory
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    settings.GSC_OAUTH_REDIRECT_URI = "http://localhost:8000/api/v1/search-console/oauth/callback/"


def _stub_response(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


@pytest.mark.django_db
def test_build_authorize_url_requests_offline_access():
    website = WebsiteFactory()
    url = oauth_service.build_authorize_url(website=website, user=website.user)

    parsed = urlparse(url)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["client_id"] == "cid"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["response_type"] == "code"
    assert "webmasters.readonly" in params["scope"]

    state = oauth_service.parse_state(params["state"])
    assert state["website_id"] == str(website.id)
    assert state["user_id"] == str(website.user.id)


@pytest.mark.django_db
def test_parse_state_rejects_tampering():
    website = WebsiteFactory()
    url = oauth_service.build_authorize_url(website=website, user=website.user)
    state = parse_qs(urlparse(url).query)["state"][0]

    with pytest.raises(signing.BadSignature):
        oauth_service.parse_state(state + "x")


@pytest.mark.django_db
def test_exchange_code_posts_authorization_grant():
    payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    with patch.object(
        oauth_service.requests, "post", return_value=_stub_response(payload)
    ) as mock_post:
        tokens = oauth_service.exchange_code("the-code")

    assert tokens["access_token"] == "at"
    args, kwargs = mock_post.call_args
    assert args[0] == "https://oauth2.googleapis.com/token"
    data = kwargs["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["client_id"] == "cid"


@pytest.mark.django_db
def test_complete_connection_creates_integration():
    website = WebsiteFactory()
    integration = oauth_service.complete_connection(
        website=website,
        tokens={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    assert integration.type == "gsc"
    assert integration.access_token == "at"
    assert integration.refresh_token == "rt"
    assert integration.is_active is True
    assert integration.token_expires_at is not None


@pytest.mark.django_db
def test_complete_connection_preserves_refresh_token_when_omitted():
    integration = GscIntegrationFactory(refresh_token="old-rt")
    updated = oauth_service.complete_connection(
        website=integration.website,
        tokens={"access_token": "new-at", "expires_in": 3600},
    )
    assert updated.pk == integration.pk
    assert updated.access_token == "new-at"
    assert updated.refresh_token == "old-rt"


@pytest.mark.django_db
def test_auto_match_prefers_domain_property():
    integration = GscIntegrationFactory(
        website__url="https://www.example.com", metadata={}
    )
    sites = [
        {"site_url": "https://example.com/", "permission_level": "siteOwner"},
        {"site_url": "sc-domain:example.com", "permission_level": "siteOwner"},
    ]
    with patch.object(oauth_service.gsc_client, "list_sites", return_value=sites):
        match = oauth_service.auto_match_property(integration)

    assert match == "sc-domain:example.com"
    integration.refresh_from_db()
    assert integration.metadata["site_url"] == "sc-domain:example.com"
    assert "pending_property_selection" not in integration.metadata


@pytest.mark.django_db
def test_auto_match_skips_unverified_and_falls_back_to_url_prefix():
    integration = GscIntegrationFactory(
        website__url="https://example.com", metadata={}
    )
    sites = [
        {"site_url": "sc-domain:example.com", "permission_level": "siteUnverifiedUser"},
        {"site_url": "https://example.com/", "permission_level": "siteFullUser"},
    ]
    with patch.object(oauth_service.gsc_client, "list_sites", return_value=sites):
        match = oauth_service.auto_match_property(integration)

    assert match == "https://example.com/"


@pytest.mark.django_db
def test_auto_match_flags_pending_selection_when_no_match():
    integration = GscIntegrationFactory(
        website__url="https://example.com", metadata={}
    )
    sites = [{"site_url": "sc-domain:unrelated.com", "permission_level": "siteOwner"}]
    with patch.object(oauth_service.gsc_client, "list_sites", return_value=sites):
        match = oauth_service.auto_match_property(integration)

    assert match is None
    integration.refresh_from_db()
    assert integration.metadata["pending_property_selection"] is True
    assert integration.metadata["available_sites"] == sites
    assert "site_url" not in integration.metadata


@pytest.mark.django_db
def test_disconnect_clears_tokens_and_survives_revoke_failure():
    integration = GscIntegrationFactory()
    with patch.object(
        oauth_service.requests, "post",
        side_effect=requests_lib.RequestException("down"),
    ):
        oauth_service.disconnect(integration)

    integration.refresh_from_db()
    assert integration.access_token == ""
    assert integration.refresh_token == ""
    assert integration.token_expires_at is None
    assert integration.is_active is False
    assert "site_url" not in integration.metadata
    # Row is retained for reconnects and metric history.
    assert Integration.objects.filter(pk=integration.pk).exists()
