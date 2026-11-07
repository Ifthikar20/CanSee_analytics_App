"""Unit tests for the GA4 API client (realtime + account listing)."""

from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

from apps.web_analytics.services import ga4_client


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


def _report(rows):
    return {
        "rows": [
            {
                "dimensionValues": [{"value": d} for d in dims],
                "metricValues": [{"value": str(m)} for m in mets],
            }
            for dims, mets in rows
        ]
    }


@pytest.mark.django_db
def test_is_configured_reflects_registry_credentials(settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
    assert ga4_client.is_configured() is False

    settings.GOOGLE_OAUTH_CLIENT_ID = "cid"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
    assert ga4_client.is_configured() is True


@pytest.mark.django_db
def test_run_realtime_report_posts_body_and_timeout():
    payload = _report([(["US"], [3])])
    with patch.object(ga4_client.requests, "post", return_value=_stub_response(payload)) as mock_post:
        out = ga4_client.run_realtime_report(
            "tok", "123", metrics=["activeUsers"], dimensions=["country"], limit=10
        )

    assert out == payload
    args, kwargs = mock_post.call_args
    assert args[0].endswith("properties/123:runRealtimeReport")
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["json"]["metrics"] == [{"name": "activeUsers"}]
    assert kwargs["json"]["dimensions"] == [{"name": "country"}]
    assert kwargs["json"]["limit"] == 10
    assert "timeout" in kwargs


@pytest.mark.django_db
def test_run_realtime_report_swallows_request_errors():
    with patch.object(
        ga4_client.requests, "post",
        side_effect=requests_lib.RequestException("boom"),
    ):
        assert ga4_client.run_realtime_report("tok", "123", metrics=["activeUsers"]) is None


@pytest.mark.django_db
def test_run_realtime_report_swallows_non_json():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(side_effect=ValueError("nope"))
    with patch.object(ga4_client.requests, "post", return_value=resp):
        assert ga4_client.run_realtime_report("tok", "123", metrics=["activeUsers"]) is None


@pytest.mark.django_db
def test_run_realtime_report_blocked_by_open_breaker():
    with patch.object(ga4_client._breaker, "allow", return_value=False), \
         patch.object(ga4_client.requests, "post") as mock_post:
        stats = {}
        out = ga4_client.run_realtime_report(
            "tok", "123", metrics=["activeUsers"], stats=stats
        )
    assert out is None
    assert stats["breaker_blocks"] == 1
    mock_post.assert_not_called()


@pytest.mark.django_db
def test_run_realtime_report_blocked_by_quota(settings):
    settings.GA4_DAILY_API_LIMIT_PER_WEBSITE = 0
    with patch.object(ga4_client.requests, "post") as mock_post:
        stats = {}
        out = ga4_client.run_realtime_report(
            "tok", "123", metrics=["activeUsers"], website_id="w1", stats=stats
        )
    assert out is None
    assert stats["quota_blocks"] == 1
    mock_post.assert_not_called()


@pytest.mark.django_db
def test_stream_filter_shape():
    assert ga4_client.stream_filter("42") == {
        "filter": {
            "fieldName": "streamId",
            "stringFilter": {"matchType": "EXACT", "value": "42"},
        }
    }


@pytest.mark.django_db
def test_build_realtime_snapshot_composes_payload():
    responses = [
        _stub_response(_report([([], [7, 21])])),                     # totals
        _stub_response(_report([(["0"], [3, 5]), (["2"], [4, 9])])),  # per minute
        _stub_response(_report([(["/pricing"], [12, 4])])),           # top pages
        _stub_response(_report([                                       # country x device
            (["United States", "desktop"], [4]),
            (["United States", "mobile"], [1]),
            (["Germany", "mobile"], [2]),
        ])),
    ]
    with patch.object(ga4_client.requests, "post", side_effect=responses):
        snap = ga4_client.build_realtime_snapshot("tok", "123", website_id="w1")

    assert snap["source"] == "ga4"
    assert snap["active_users"] == 7
    assert snap["page_views"] == 21
    assert snap["window_minutes"] == 30
    assert len(snap["per_minute"]) == 30
    # Series is oldest -> newest; minute 0 is the last entry.
    assert snap["per_minute"][-1] == {"minutes_ago": 0, "active_users": 3, "page_views": 5}
    assert snap["per_minute"][-3] == {"minutes_ago": 2, "active_users": 4, "page_views": 9}
    assert snap["per_minute"][0] == {"minutes_ago": 29, "active_users": 0, "page_views": 0}
    assert snap["top_pages"] == [{"page": "/pricing", "page_views": 12, "active_users": 4}]
    assert snap["countries"][0] == {"country": "United States", "active_users": 5}
    assert {"device": "mobile", "active_users": 3} in snap["devices"]


@pytest.mark.django_db
def test_build_realtime_snapshot_none_when_totals_fail():
    with patch.object(
        ga4_client.requests, "post",
        side_effect=requests_lib.RequestException("boom"),
    ):
        assert ga4_client.build_realtime_snapshot("tok", "123", website_id="w1") is None


@pytest.mark.django_db
def test_run_report_posts_date_ranges_and_body():
    payload = _report([(["20260824"], [5, 12])])
    with patch.object(ga4_client.requests, "post", return_value=_stub_response(payload)) as mock_post:
        out = ga4_client.run_report(
            "tok",
            "123",
            metrics=["totalUsers", "screenPageViews"],
            dimensions=["date"],
            date_ranges=[("2026-07-26", "2026-08-24"), ("2026-06-27", "2026-07-25")],
            limit=100,
            order_by_metric="screenPageViews",
        )

    assert out == payload
    args, kwargs = mock_post.call_args
    assert args[0].endswith("properties/123:runReport")
    body = kwargs["json"]
    assert body["dateRanges"] == [
        {"startDate": "2026-07-26", "endDate": "2026-08-24"},
        {"startDate": "2026-06-27", "endDate": "2026-07-25"},
    ]
    assert body["metrics"] == [{"name": "totalUsers"}, {"name": "screenPageViews"}]
    assert body["dimensions"] == [{"name": "date"}]
    assert body["orderBys"] == [{"desc": True, "metric": {"metricName": "screenPageViews"}}]
    assert "timeout" in kwargs


@pytest.mark.django_db
def test_run_report_requires_date_ranges_and_swallows_errors():
    assert ga4_client.run_report("tok", "123", metrics=["totalUsers"], date_ranges=[]) is None
    with patch.object(
        ga4_client.requests, "post",
        side_effect=requests_lib.RequestException("boom"),
    ):
        assert ga4_client.run_report(
            "tok", "123", metrics=["totalUsers"], date_ranges=[("2026-08-01", "2026-08-24")]
        ) is None


@pytest.mark.django_db
def test_run_report_blocked_by_quota(settings):
    settings.GA4_DAILY_API_LIMIT_PER_WEBSITE = 0
    with patch.object(ga4_client.requests, "post") as mock_post:
        out = ga4_client.run_report(
            "tok", "123", metrics=["totalUsers"],
            date_ranges=[("2026-08-01", "2026-08-24")], website_id="w1",
        )
    assert out is None
    mock_post.assert_not_called()


@pytest.mark.django_db
def test_list_account_summaries_flattens_and_paginates():
    page1 = _stub_response({
        "accountSummaries": [{
            "displayName": "Acct A",
            "propertySummaries": [
                {"property": "properties/111", "displayName": "Site One"},
                {"property": "properties/222", "displayName": "Site Two"},
            ],
        }],
        "nextPageToken": "t2",
    })
    page2 = _stub_response({
        "accountSummaries": [{
            "displayName": "Acct B",
            "propertySummaries": [{"property": "properties/333", "displayName": "Site Three"}],
        }],
    })
    with patch.object(ga4_client.requests, "get", side_effect=[page1, page2]) as mock_get:
        props = ga4_client.list_account_summaries("tok", website_id="w1")

    assert [p["property_id"] for p in props] == ["111", "222", "333"]
    assert props[0]["account_name"] == "Acct A"
    assert mock_get.call_count == 2
