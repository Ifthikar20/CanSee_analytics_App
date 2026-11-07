"""Tests for the Google login endpoint (POST /api/v1/auth/google/).

Public sign-up is closed: Google sign-in only works for emails that
already have a Cansee account. Unknown emails are rejected with 403
and no account is created.
"""

from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.services import oauth_service
from apps.accounts.tests.factories import UserFactory

REDIRECT_URI = "http://localhost:5173/auth/google/callback"


def _stub_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _google_mocks(userinfo):
    token_post = patch.object(
        oauth_service.requests, "post",
        return_value=_stub_response({"access_token": "google-at"}),
    )
    userinfo_get = patch.object(
        oauth_service.requests, "get",
        return_value=_stub_response(userinfo),
    )
    return token_post, userinfo_get


@pytest.mark.django_db
class TestGoogleOAuthView:
    def test_logs_in_existing_user(self, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        existing = UserFactory(email="existing.google@test.com")
        token_post, userinfo_get = _google_mocks(
            {"email": "existing.google@test.com", "name": "Existing"}
        )
        with token_post as mock_post, userinfo_get:
            response = APIClient().post(
                "/api/v1/auth/google/",
                {"code": "auth-code", "redirect_uri": REDIRECT_URI},
                format="json",
            )

        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["access"]
        assert payload["user"]["id"] == str(existing.id)
        assert payload["user"]["email"] == "existing.google@test.com"
        # Refresh cookie is set like password login.
        assert any("refresh" in c.lower() for c in response.cookies.keys())
        # No duplicate account.
        assert User.objects.filter(email__iexact="existing.google@test.com").count() == 1

        # The code exchange used our credentials and the SPA redirect URI.
        data = mock_post.call_args.kwargs["data"]
        assert data["code"] == "auth-code"
        assert data["redirect_uri"] == REDIRECT_URI
        assert data["grant_type"] == "authorization_code"

    def test_unknown_google_email_rejected(self):
        token_post, userinfo_get = _google_mocks(
            {"email": "stranger@test.com", "name": "Stranger"}
        )
        with token_post, userinfo_get:
            response = APIClient().post(
                "/api/v1/auth/google/",
                {"code": "auth-code", "redirect_uri": REDIRECT_URI},
                format="json",
            )

        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "permission_denied"
        assert "No Cansee account" in body["error"]["message"]
        # Sign-up stays closed: no account was created.
        assert not User.objects.filter(email__iexact="stranger@test.com").exists()

    def test_missing_code_returns_400(self):
        response = APIClient().post("/api/v1/auth/google/", {}, format="json")
        assert response.status_code == 400
