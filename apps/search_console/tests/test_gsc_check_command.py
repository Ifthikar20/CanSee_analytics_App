"""Tests for the gsc_check management command (mocked Google HTTP)."""

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.search_console.tests.factories import GscIntegrationFactory
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"


def _resp(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _run(*args):
    out = StringIO()
    call_command("gsc_check", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_stops_cleanly_without_website_arg():
    output = _run()
    assert "[PASS] OAuth credentials configured" in output
    assert "[SKIP] No --website" in output


@pytest.mark.django_db
def test_fails_when_credentials_missing(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
    output = _run()
    assert "[FAIL] OAuth credentials missing" in output


@pytest.mark.django_db
def test_reports_not_connected_website():
    website = WebsiteFactory(url="https://example.com", name="Example")
    output = _run("--website", str(website.id))
    assert "[PASS] Website found" in output
    assert "[FAIL] No GSC connection" in output


@pytest.mark.django_db
def test_full_happy_path_with_mocked_google():
    GscIntegrationFactory(
        website__url="https://example.com",
        token_expires_at=timezone.now() + timedelta(minutes=5),
    )

    refresh_resp = _resp({"access_token": "fresh", "expires_in": 3600})
    sites_resp = _resp({
        "siteEntry": [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
        ]
    })
    query_resp = _resp({
        "rows": [
            {"keys": ["2026-06-30"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 4.0},
        ]
    })

    def fake_post(url, **kwargs):
        if "oauth2" in url:
            return refresh_resp
        return query_resp

    with patch(
        "apps.search_console.services.oauth_service.requests.post",
        side_effect=fake_post,
    ), patch(
        "apps.search_console.management.commands.gsc_check.requests.get",
        return_value=sites_resp,
    ), patch(
        "apps.search_console.management.commands.gsc_check.requests.post",
        side_effect=fake_post,
    ):
        output = _run("--website", "example.com")

    assert "[PASS] Integration row healthy" in output
    assert "[PASS] Access token refreshed" in output
    assert "[PASS] Search Console API reachable" in output
    assert "[PASS] Search Analytics query OK" in output
    assert "All checks passed" in output


@pytest.mark.django_db
def test_api_disabled_hint():
    integration = GscIntegrationFactory(
        website__url="https://example.com",
        token_expires_at=timezone.now() + timedelta(hours=2),
    )
    disabled = _resp(
        {"error": {"message": "Google Search Console API has not been used in project 654 before or it is disabled."}},
        status_code=403,
    )
    refresh_ok = _resp({"access_token": "fresh", "expires_in": 3600})
    with patch(
        "apps.search_console.services.oauth_service.requests.post",
        return_value=refresh_ok,
    ), patch(
        "apps.search_console.management.commands.gsc_check.requests.get",
        return_value=disabled,
    ):
        output = _run("--website", str(integration.website_id))

    assert "[FAIL] Property listing returned HTTP 403" in output
    assert "Enable the Google Search Console API" in output


@pytest.mark.django_db
def test_invalid_grant_hint():
    integration = GscIntegrationFactory(
        website__url="https://example.com",
        token_expires_at=timezone.now() + timedelta(minutes=5),
    )
    rejected = _resp({}, status_code=400, text='{"error": "invalid_grant"}')
    with patch(
        "apps.search_console.services.oauth_service.requests.post",
        return_value=rejected,
    ):
        output = _run("--website", str(integration.website_id))

    assert "[FAIL] Google rejected the refresh token" in output
    assert "Test users" in output
