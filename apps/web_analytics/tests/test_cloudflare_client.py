"""Unit tests for the Cloudflare zone analytics client."""

from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

from apps.web_analytics.services import cloudflare_client


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def _stub_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def test_verify_token_true_only_when_active():
    ok = _stub_response({"success": True, "result": {"status": "active"}})
    with patch.object(cloudflare_client.requests, "get", return_value=ok):
        assert cloudflare_client.verify_token("tok") is True

    disabled = _stub_response({"success": True, "result": {"status": "disabled"}})
    with patch.object(cloudflare_client.requests, "get", return_value=disabled):
        assert cloudflare_client.verify_token("tok") is False

    with patch.object(
        cloudflare_client.requests, "get",
        side_effect=requests_lib.RequestException("down"),
    ):
        assert cloudflare_client.verify_token("tok") is False

    assert cloudflare_client.verify_token("") is False


def test_list_zones_paginates_and_parses():
    page1 = _stub_response({
        "result": [{"id": "z1", "name": "example.com", "status": "active", "paused": False}],
        "result_info": {"total_pages": 2},
    })
    page2 = _stub_response({
        "result": [{"id": "z2", "name": "other.io", "status": "active", "paused": False}],
        "result_info": {"total_pages": 2},
    })
    with patch.object(cloudflare_client.requests, "get", side_effect=[page1, page2]):
        zones = cloudflare_client.list_zones("tok")

    assert [z["id"] for z in zones] == ["z1", "z2"]


def test_list_zones_swallows_errors():
    with patch.object(
        cloudflare_client.requests, "get",
        side_effect=requests_lib.RequestException("down"),
    ):
        assert cloudflare_client.list_zones("tok") == []


def test_graphql_errors_yield_none():
    resp = _stub_response({"data": None, "errors": [{"message": "bad zone"}]})
    with patch.object(cloudflare_client.requests, "post", return_value=resp):
        assert cloudflare_client.build_zone_snapshot("tok", "z1") is None


def test_snapshot_unsamples_and_sorts():
    data = {
        "viewer": {
            "zones": [{
                "perMinute": [
                    {
                        "count": 10,
                        "avg": {"sampleInterval": 4},
                        "sum": {"visits": 5, "edgeResponseBytes": 1000},
                        "dimensions": {"datetimeMinute": "2026-08-24T10:01:00Z"},
                    },
                    {
                        "count": 2,
                        "avg": {"sampleInterval": 1},
                        "sum": {"visits": 2, "edgeResponseBytes": 300},
                        "dimensions": {"datetimeMinute": "2026-08-24T10:00:00Z"},
                    },
                ],
                "totals": [{
                    "count": 100,
                    "avg": {"sampleInterval": 2},
                    "sum": {"visits": 40, "edgeResponseBytes": 9000},
                }],
                "countries": [{
                    "count": 50,
                    "avg": {"sampleInterval": 2},
                    "sum": {"visits": 30},
                    "dimensions": {"clientCountryName": "US"},
                }],
            }]
        }
    }
    resp = _stub_response({"data": data})
    with patch.object(cloudflare_client.requests, "post", return_value=resp):
        snap = cloudflare_client.build_zone_snapshot("tok", "z1")

    assert snap["source"] == "cloudflare"
    assert snap["sampled"] is True
    # Sorted oldest -> newest and multiplied by sampleInterval.
    assert snap["per_minute"][0]["minute"] == "2026-08-24T10:00:00Z"
    assert snap["per_minute"][1] == {
        "minute": "2026-08-24T10:01:00Z", "requests": 40, "visits": 20, "bytes": 4000,
    }
    assert snap["totals_24h"] == {"requests": 200, "visits": 80, "bytes": 18000}
    assert snap["countries"] == [{"country": "US", "requests": 100, "visits": 60, "bytes": 0}]


def test_snapshot_none_when_zone_missing():
    resp = _stub_response({"data": {"viewer": {"zones": []}}})
    with patch.object(cloudflare_client.requests, "post", return_value=resp):
        assert cloudflare_client.build_zone_snapshot("tok", "z1") is None
