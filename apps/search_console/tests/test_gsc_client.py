"""Unit tests for the Search Console API client."""

from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

from apps.search_console.services import gsc_client


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def _stub_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


@pytest.mark.django_db
def test_is_configured_reflects_registry_credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
    assert gsc_client.is_configured() is False

    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    assert gsc_client.is_configured() is True


@pytest.mark.django_db
def test_list_sites_parses_entries():
    payload = {
        "siteEntry": [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
            {"siteUrl": "https://other.com/", "permissionLevel": "siteFullUser"},
            {"permissionLevel": "siteOwner"},  # no siteUrl -> dropped
        ]
    }
    with patch.object(gsc_client.requests, "get", return_value=_stub_response(payload)) as mock_get:
        out = gsc_client.list_sites("tok")

    assert out == [
        {"site_url": "sc-domain:example.com", "permission_level": "siteOwner"},
        {"site_url": "https://other.com/", "permission_level": "siteFullUser"},
    ]
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.django_db
def test_list_sites_swallows_request_errors():
    with patch.object(
        gsc_client.requests, "get",
        side_effect=requests_lib.RequestException("boom"),
    ):
        assert gsc_client.list_sites("tok") == []


@pytest.mark.django_db
def test_query_sends_correct_body_and_encodes_site_url():
    payload = {"rows": [{"keys": ["2026-06-01"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 4.2}]}
    with patch.object(gsc_client.requests, "post", return_value=_stub_response(payload)) as mock_post:
        rows = gsc_client.query_search_analytics(
            "tok",
            "sc-domain:example.com",
            start_date="2026-06-01",
            end_date="2026-06-07",
            dimensions=["date"],
            row_limit=100,
        )

    assert len(rows) == 1
    args, kwargs = mock_post.call_args
    assert "sc-domain%3Aexample.com" in args[0]
    body = kwargs["json"]
    assert body["startDate"] == "2026-06-01"
    assert body["endDate"] == "2026-06-07"
    assert body["dimensions"] == ["date"]
    assert body["rowLimit"] == 100
    assert body["startRow"] == 0


@pytest.mark.django_db
def test_query_returns_empty_on_request_error_with_stats():
    stats = {}
    with patch.object(
        gsc_client.requests, "post",
        side_effect=requests_lib.RequestException("boom"),
    ):
        rows = gsc_client.query_search_analytics(
            "tok", "sc-domain:example.com",
            start_date="2026-06-01", end_date="2026-06-07",
            dimensions=["date"], stats=stats,
        )
    assert rows == []
    assert stats["errors"] == 1


@pytest.mark.django_db
def test_query_blocked_by_quota(settings):
    settings.GSC_DAILY_API_LIMIT_PER_USER = 0
    stats = {}
    with patch.object(gsc_client.requests, "post") as mock_post:
        rows = gsc_client.query_search_analytics(
            "tok", "sc-domain:example.com",
            start_date="2026-06-01", end_date="2026-06-07",
            dimensions=["date"], user_id="u1", stats=stats,
        )
    assert rows == []
    assert stats["quota_blocks"] == 1
    mock_post.assert_not_called()


@pytest.mark.django_db
def test_query_blocked_when_breaker_open():
    stats = {}
    with patch.object(gsc_client._breaker, "allow", return_value=False):
        with patch.object(gsc_client.requests, "post") as mock_post:
            rows = gsc_client.query_search_analytics(
                "tok", "sc-domain:example.com",
                start_date="2026-06-01", end_date="2026-06-07",
                dimensions=["date"], stats=stats,
            )
    assert rows == []
    assert stats["breaker_blocks"] == 1
    mock_post.assert_not_called()


@pytest.mark.django_db
def test_query_all_rows_paginates_until_short_page():
    calls = []

    def fake_query(access_token, site_url, **kwargs):
        calls.append((kwargs["start_row"], kwargs["row_limit"]))
        if kwargs["start_row"] == 0:
            # Full first page (API cap 25000) forces a second request.
            return [{"keys": ["2026-06-01"], "clicks": 1}] * kwargs["row_limit"]
        return [{"keys": ["2026-06-02"], "clicks": 1}] * 5  # short page ends it

    with patch.object(gsc_client, "query_search_analytics", side_effect=fake_query):
        rows = gsc_client.query_all_rows(
            "tok", "sc-domain:example.com",
            start_date="2026-06-01", end_date="2026-06-28",
            dimensions=["date", "query"], max_rows=30000,
        )

    assert calls == [(0, 25000), (25000, 5000)]
    assert len(rows) == 25005


@pytest.mark.django_db
def test_query_all_rows_respects_max_rows():
    def fake_query(access_token, site_url, **kwargs):
        return [{"keys": ["2026-06-01"], "clicks": 1}] * kwargs["row_limit"]

    with patch.object(gsc_client, "query_search_analytics", side_effect=fake_query):
        rows = gsc_client.query_all_rows(
            "tok", "sc-domain:example.com",
            start_date="2026-06-01", end_date="2026-06-28",
            dimensions=["date", "query"], max_rows=250,
        )
    assert len(rows) == 250
