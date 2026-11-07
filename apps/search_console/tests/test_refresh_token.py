"""Tests for GSC token refresh and its registry wiring."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.search_console.services import oauth_service
from apps.search_console.tests.factories import GscIntegrationFactory


@pytest.fixture(autouse=True)
def _credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"


def _stub_response(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def test_registry_has_gsc_refresh_fn_wired():
    from core.integrations import get_registry

    cfg = get_registry().get("gsc")
    assert cfg is not None
    assert cfg.refresh_token_fn is oauth_service.refresh_access_token


@pytest.mark.django_db
def test_refresh_updates_access_token_and_expiry():
    integration = GscIntegrationFactory(
        access_token="stale",
        token_expires_at=timezone.now() + timedelta(minutes=5),
    )
    payload = {"access_token": "fresh", "expires_in": 3600}
    with patch.object(
        oauth_service.requests, "post", return_value=_stub_response(payload)
    ) as mock_post:
        oauth_service.refresh_access_token(integration)

    integration.refresh_from_db()
    assert integration.access_token == "fresh"
    assert integration.token_expires_at > timezone.now() + timedelta(minutes=45)
    data = mock_post.call_args.kwargs["data"]
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "refresh-token"


@pytest.mark.django_db
def test_refresh_invalid_grant_deactivates_integration():
    integration = GscIntegrationFactory(
        token_expires_at=timezone.now() + timedelta(minutes=5),
    )
    resp = _stub_response({}, status_code=400, text='{"error": "invalid_grant"}')
    with patch.object(oauth_service.requests, "post", return_value=resp):
        oauth_service.refresh_access_token(integration)

    integration.refresh_from_db()
    assert integration.is_active is False
    assert "revoked_at" in integration.metadata


@pytest.mark.django_db
def test_refresh_without_refresh_token_deactivates():
    integration = GscIntegrationFactory(refresh_token="")
    oauth_service.refresh_access_token(integration)
    integration.refresh_from_db()
    assert integration.is_active is False


@pytest.mark.django_db
def test_beat_task_refreshes_expiring_gsc_token():
    """End-to-end: the existing refresh_expiring_tokens task must pick up
    the gsc integration through the registry-wired refresh function."""
    from apps.websites.tasks import refresh_expiring_tokens

    integration = GscIntegrationFactory(
        access_token="stale",
        token_expires_at=timezone.now() + timedelta(minutes=5),
    )
    payload = {"access_token": "fresh", "expires_in": 3600}
    with patch.object(
        oauth_service.requests, "post", return_value=_stub_response(payload)
    ):
        result = refresh_expiring_tokens()

    integration.refresh_from_db()
    assert integration.access_token == "fresh"
    assert result["refreshed"] >= 1
