"""Unit tests for the GA4 OAuth service."""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import requests as requests_lib
from django.core import signing

from apps.web_analytics.services import ga4_oauth
from apps.web_analytics.tests.factories import Ga4IntegrationFactory
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    settings.GA4_OAUTH_REDIRECT_URI = (
        "http://localhost:8000/api/v1/web-analytics/ga4/oauth/callback/"
    )


def _stub_response(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


@pytest.mark.django_db
def test_build_authorize_url_requests_offline_analytics_scope():
    website = WebsiteFactory()
    url = ga4_oauth.build_authorize_url(website=website, user=website.user)

    parsed = urlparse(url)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["client_id"] == "cid"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert "analytics.readonly" in params["scope"]
    # Deliberate deviation from GSC: no scope aggregation.
    assert "include_granted_scopes" not in params

    state = ga4_oauth.parse_state(params["state"])
    assert state["website_id"] == str(website.id)
    assert state["user_id"] == str(website.user.id)


@pytest.mark.django_db
def test_parse_state_rejects_tampering():
    website = WebsiteFactory()
    url = ga4_oauth.build_authorize_url(website=website, user=website.user)
    state = parse_qs(urlparse(url).query)["state"][0]

    with pytest.raises(signing.BadSignature):
        ga4_oauth.parse_state(state + "x")


@pytest.mark.django_db
def test_complete_connection_creates_ga_integration():
    website = WebsiteFactory()
    integration = ga4_oauth.complete_connection(
        website=website,
        tokens={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    assert integration.type == "ga"
    assert integration.access_token == "at"
    assert integration.refresh_token == "rt"
    assert integration.is_active is True
    assert integration.token_expires_at is not None


@pytest.mark.django_db
def test_complete_connection_preserves_refresh_token_when_omitted():
    integration = Ga4IntegrationFactory(refresh_token="old-rt")
    updated = ga4_oauth.complete_connection(
        website=integration.website,
        tokens={"access_token": "new-at", "expires_in": 3600},
    )
    assert updated.pk == integration.pk
    assert updated.access_token == "new-at"
    assert updated.refresh_token == "old-rt"


@pytest.mark.django_db
def test_auto_select_picks_single_property():
    integration = Ga4IntegrationFactory(metadata={})
    props = [{"property_id": "111", "display_name": "Only", "account_name": "Acct"}]
    with patch.object(ga4_oauth.ga4_client, "list_account_summaries", return_value=props):
        selected = ga4_oauth.auto_select_property(integration)

    assert selected == "111"
    integration.refresh_from_db()
    assert integration.metadata["property_id"] == "111"
    assert integration.metadata["property_display_name"] == "Only"
    assert "pending_property_selection" not in integration.metadata


@pytest.mark.django_db
def test_auto_select_flags_pending_when_ambiguous():
    integration = Ga4IntegrationFactory(metadata={})
    props = [
        {"property_id": "111", "display_name": "One", "account_name": "Acct"},
        {"property_id": "222", "display_name": "Two", "account_name": "Acct"},
    ]
    with patch.object(ga4_oauth.ga4_client, "list_account_summaries", return_value=props):
        selected = ga4_oauth.auto_select_property(integration)

    assert selected is None
    integration.refresh_from_db()
    assert integration.metadata["pending_property_selection"] is True
    assert integration.metadata["available_properties"] == props
    assert "property_id" not in integration.metadata


@pytest.mark.django_db
def test_select_property_rejects_unknown_id():
    integration = Ga4IntegrationFactory(
        metadata={"available_properties": [
            {"property_id": "111", "display_name": "One", "account_name": "Acct"}
        ]},
    )
    assert ga4_oauth.select_property(integration, "999") is False
    assert ga4_oauth.select_property(integration, "111") is True
    integration.refresh_from_db()
    assert integration.metadata["property_id"] == "111"


@pytest.mark.django_db
def test_disconnect_revokes_when_no_other_google_integration():
    integration = Ga4IntegrationFactory()
    with patch.object(ga4_oauth.requests, "post") as mock_post:
        ga4_oauth.disconnect(integration)

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == ga4_oauth.REVOKE_ENDPOINT
    integration.refresh_from_db()
    assert integration.access_token == ""
    assert integration.is_active is False
    assert "property_id" not in integration.metadata


@pytest.mark.django_db
def test_disconnect_skips_revoke_while_gsc_active():
    """Google keeps one grant per user+client: revoking the GA4 token
    would also kill the GSC connection, so it must be skipped."""
    integration = Ga4IntegrationFactory()
    Integration.objects.create(
        website=integration.website, type="gsc",
        access_token="gsc-at", refresh_token="gsc-rt", is_active=True,
    )
    with patch.object(ga4_oauth.requests, "post") as mock_post:
        ga4_oauth.disconnect(integration)

    mock_post.assert_not_called()
    integration.refresh_from_db()
    assert integration.is_active is False
    assert integration.access_token == ""


@pytest.mark.django_db
def test_disconnect_survives_revoke_failure():
    integration = Ga4IntegrationFactory()
    with patch.object(
        ga4_oauth.requests, "post",
        side_effect=requests_lib.RequestException("down"),
    ):
        ga4_oauth.disconnect(integration)

    integration.refresh_from_db()
    assert integration.is_active is False


@pytest.mark.django_db
def test_refresh_invalid_grant_deactivates():
    integration = Ga4IntegrationFactory()
    resp = _stub_response({}, status_code=400, text='{"error": "invalid_grant"}')
    with patch.object(ga4_oauth.requests, "post", return_value=resp):
        ga4_oauth.refresh_access_token(integration)

    integration.refresh_from_db()
    assert integration.is_active is False
    assert "revoked_at" in integration.metadata


@pytest.mark.django_db
def test_refresh_updates_access_token():
    integration = Ga4IntegrationFactory()
    resp = _stub_response({"access_token": "fresh", "expires_in": 3600})
    with patch.object(ga4_oauth.requests, "post", return_value=resp):
        ga4_oauth.refresh_access_token(integration)

    integration.refresh_from_db()
    assert integration.access_token == "fresh"
    assert integration.is_active is True
