"""Tests for GSC data endpoints (summary, timeseries, queries, pages)."""

from datetime import date

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.search_console.tests.factories import (
    GscDailyTotalFactory,
    GscPageStatFactory,
    GscQueryStatFactory,
)
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def auth_client():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


def _seed_totals(website):
    # Current period: 2026-06-08..2026-06-14; previous: 2026-06-01..2026-06-07.
    for day in range(1, 8):
        GscDailyTotalFactory(
            website=website, date=date(2026, 6, day),
            clicks=10, impressions=100, position=8.0,
        )
    for day in range(8, 15):
        GscDailyTotalFactory(
            website=website, date=date(2026, 6, day),
            clicks=20, impressions=200, position=4.0,
        )


@pytest.mark.django_db
class TestSummary:
    def test_totals_and_deltas(self, auth_client):
        client, user, website = auth_client
        _seed_totals(website)
        response = client.get(
            f"/api/v1/search-console/{website.id}/summary/",
            {"start": "2026-06-08", "end": "2026-06-14"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["clicks"] == 140
        assert data["impressions"] == 1400
        assert data["ctr"] == pytest.approx(0.1)
        assert data["position"] == pytest.approx(4.0)
        assert data["previous"]["clicks"] == 70
        assert data["deltas"]["clicks"] == pytest.approx(1.0)  # doubled
        assert data["deltas"]["position"] == pytest.approx(-0.5)

    def test_rejects_inverted_range(self, auth_client):
        client, user, website = auth_client
        response = client.get(
            f"/api/v1/search-console/{website.id}/summary/",
            {"start": "2026-06-14", "end": "2026-06-08"},
        )
        assert response.status_code == 400

    def test_rejects_oversized_range(self, auth_client):
        client, user, website = auth_client
        response = client.get(
            f"/api/v1/search-console/{website.id}/summary/",
            {"start": "2020-01-01", "end": "2026-06-08"},
        )
        assert response.status_code == 400

    def test_404_for_other_users_website(self, auth_client):
        client, user, website = auth_client
        other = WebsiteFactory()
        response = client.get(f"/api/v1/search-console/{other.id}/summary/")
        assert response.status_code == 404


@pytest.mark.django_db
class TestTimeseries:
    def test_ordered_series(self, auth_client):
        client, user, website = auth_client
        GscDailyTotalFactory(website=website, date=date(2026, 6, 3), clicks=3)
        GscDailyTotalFactory(website=website, date=date(2026, 6, 1), clicks=1)
        GscDailyTotalFactory(website=website, date=date(2026, 6, 2), clicks=2)
        response = client.get(
            f"/api/v1/search-console/{website.id}/timeseries/",
            {"start": "2026-06-01", "end": "2026-06-07"},
        )
        series = response.json()["data"]["series"]
        assert [row["date"] for row in series] == ["2026-06-01", "2026-06-02", "2026-06-03"]
        assert [row["clicks"] for row in series] == [1, 2, 3]


@pytest.mark.django_db
class TestQueriesAndPages:
    def _seed_queries(self, website):
        # "big query" across two days; "small query" once.
        GscQueryStatFactory(
            website=website, date=date(2026, 6, 8), query="big query",
            clicks=10, impressions=100, position=4.0,
        )
        GscQueryStatFactory(
            website=website, date=date(2026, 6, 9), query="big query",
            clicks=30, impressions=300, position=8.0,
        )
        GscQueryStatFactory(
            website=website, date=date(2026, 6, 8), query="small query",
            clicks=1, impressions=10, position=20.0,
        )
        # Previous period row for delta computation.
        GscQueryStatFactory(
            website=website, date=date(2026, 6, 1), query="big query",
            clicks=20, impressions=200, position=10.0,
        )

    def test_aggregation_math(self, auth_client):
        client, user, website = auth_client
        self._seed_queries(website)
        response = client.get(
            f"/api/v1/search-console/{website.id}/queries/",
            {"start": "2026-06-08", "end": "2026-06-14"},
        )
        data = response.json()["data"]
        assert data["count"] == 2
        top = data["rows"][0]
        assert top["query"] == "big query"
        assert top["clicks"] == 40
        assert top["impressions"] == 400
        assert top["ctr"] == pytest.approx(0.1)
        # Impressions-weighted position: (4*100 + 8*300) / 400 = 7.0
        assert top["position"] == pytest.approx(7.0)
        # Deltas vs previous week: clicks 40 vs 20 -> +1.0; position 7 vs 10 -> -3.
        assert top["prev_clicks"] == 20
        assert top["clicks_delta"] == pytest.approx(1.0)
        assert top["position_delta"] == pytest.approx(-3.0)

    def test_order_whitelist(self, auth_client):
        client, user, website = auth_client
        response = client.get(
            f"/api/v1/search-console/{website.id}/queries/",
            {"order": "evil"},
        )
        assert response.status_code == 400

    def test_order_ascending_position(self, auth_client):
        client, user, website = auth_client
        self._seed_queries(website)
        response = client.get(
            f"/api/v1/search-console/{website.id}/queries/",
            {"start": "2026-06-08", "end": "2026-06-14", "order": "position"},
        )
        results = response.json()["data"]["rows"]
        assert results[0]["query"] == "big query"  # position 7 < 20

    def test_search_filter(self, auth_client):
        client, user, website = auth_client
        self._seed_queries(website)
        response = client.get(
            f"/api/v1/search-console/{website.id}/queries/",
            {"start": "2026-06-08", "end": "2026-06-14", "search": "small"},
        )
        data = response.json()["data"]
        assert data["count"] == 1
        assert data["rows"][0]["query"] == "small query"

    def test_pagination(self, auth_client):
        client, user, website = auth_client
        self._seed_queries(website)
        response = client.get(
            f"/api/v1/search-console/{website.id}/queries/",
            {"start": "2026-06-08", "end": "2026-06-14", "limit": 1, "offset": 1},
        )
        data = response.json()["data"]
        assert data["count"] == 2
        assert len(data["rows"]) == 1
        assert data["rows"][0]["query"] == "small query"

    def test_pages_endpoint(self, auth_client):
        client, user, website = auth_client
        GscPageStatFactory(
            website=website, date=date(2026, 6, 8),
            page="https://example.com/pricing", clicks=5, impressions=50,
        )
        response = client.get(
            f"/api/v1/search-console/{website.id}/pages/",
            {"start": "2026-06-08", "end": "2026-06-14"},
        )
        data = response.json()["data"]
        assert data["count"] == 1
        assert data["rows"][0]["page"] == "https://example.com/pricing"
