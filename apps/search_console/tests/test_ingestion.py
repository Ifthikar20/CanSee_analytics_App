"""Tests for the Search Analytics ingestion service."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.search_console.models import GscDailyTotal, GscPageStat, GscQueryStat
from apps.search_console.services import ingestion_service
from apps.search_console.tests.factories import (
    GscDailyTotalFactory,
    GscIntegrationFactory,
)

TODAY = date(2026, 7, 1)


def _rows(dimension):
    if dimension == ["date"]:
        return [
            {"keys": ["2026-06-01"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 5.0},
            {"keys": ["2026-06-02"], "clicks": 20, "impressions": 200, "ctr": 0.1, "position": 4.0},
        ]
    if dimension == ["date", "query"]:
        return [
            {"keys": ["2026-06-01", "best crm software"], "clicks": 3, "impressions": 40, "ctr": 0.075, "position": 7.1},
            {"keys": ["2026-06-01", ""], "clicks": 1, "impressions": 5, "ctr": 0.2, "position": 2.0},  # dropped
        ]
    return [
        {"keys": ["2026-06-01", "https://example.com/pricing"], "clicks": 2, "impressions": 30, "ctr": 0.066, "position": 6.0},
    ]


def _fake_query_all_rows(access_token, site_url, *, dimensions, **kwargs):
    return _rows(dimensions)


@pytest.mark.django_db
def test_sync_window_upserts_all_dimensions():
    integration = GscIntegrationFactory()
    with patch.object(ingestion_service.gsc_client, "query_all_rows", side_effect=_fake_query_all_rows):
        result = ingestion_service.sync_window(
            integration, start_date=date(2026, 6, 1), end_date=date(2026, 6, 7)
        )

    assert result["totals"] == 2
    assert result["queries"] == 1  # empty query key dropped
    assert result["pages"] == 1
    assert GscDailyTotal.objects.filter(website=integration.website).count() == 2
    assert GscQueryStat.objects.filter(website=integration.website).count() == 1
    assert GscPageStat.objects.filter(website=integration.website).count() == 1
    integration.refresh_from_db()
    assert integration.last_synced_at is not None


@pytest.mark.django_db
def test_sync_window_is_idempotent_and_updates_metrics():
    integration = GscIntegrationFactory()
    with patch.object(ingestion_service.gsc_client, "query_all_rows", side_effect=_fake_query_all_rows):
        ingestion_service.sync_window(
            integration, start_date=date(2026, 6, 1), end_date=date(2026, 6, 7)
        )

    revised = [
        {"keys": ["2026-06-01"], "clicks": 15, "impressions": 150, "ctr": 0.1, "position": 4.5},
        {"keys": ["2026-06-02"], "clicks": 20, "impressions": 200, "ctr": 0.1, "position": 4.0},
    ]

    def revised_rows(access_token, site_url, *, dimensions, **kwargs):
        return revised if dimensions == ["date"] else _rows(dimensions)

    with patch.object(ingestion_service.gsc_client, "query_all_rows", side_effect=revised_rows):
        ingestion_service.sync_window(
            integration, start_date=date(2026, 6, 1), end_date=date(2026, 6, 7)
        )

    totals = GscDailyTotal.objects.filter(website=integration.website)
    assert totals.count() == 2  # no duplicates
    assert totals.get(date=date(2026, 6, 1)).clicks == 15  # revised in place


@pytest.mark.django_db
def test_sync_window_skips_without_site_url():
    integration = GscIntegrationFactory(metadata={})
    result = ingestion_service.sync_window(
        integration, start_date=date(2026, 6, 1), end_date=date(2026, 6, 7)
    )
    assert result == {"skipped": "no_site_url"}


@pytest.mark.django_db
def test_sync_window_refreshes_token_when_needed():
    integration = GscIntegrationFactory(
        token_expires_at=timezone.now() + timedelta(minutes=1)
    )
    with patch.object(ingestion_service.oauth_service, "refresh_access_token") as mock_refresh:
        with patch.object(ingestion_service.gsc_client, "query_all_rows", return_value=[]):
            ingestion_service.sync_window(
                integration, start_date=date(2026, 6, 1), end_date=date(2026, 6, 7)
            )
    mock_refresh.assert_called_once()


@pytest.mark.django_db
def test_compute_sync_window_first_sync_backfills(settings):
    settings.GSC_BACKFILL_DAYS = 28
    integration = GscIntegrationFactory(last_synced_at=None)
    start, end = ingestion_service.compute_sync_window(integration, today=TODAY)
    assert end == TODAY - timedelta(days=3)
    assert (end - start).days + 1 == 28


@pytest.mark.django_db
def test_compute_sync_window_incremental_lookback(settings):
    settings.GSC_SYNC_LOOKBACK_DAYS = 7
    integration = GscIntegrationFactory(last_synced_at=timezone.now())
    start, end = ingestion_service.compute_sync_window(integration, today=TODAY)
    assert end == TODAY - timedelta(days=3)
    assert (end - start).days + 1 == 7


@pytest.mark.django_db
def test_compute_sync_window_treats_existing_data_as_incremental(settings):
    settings.GSC_SYNC_LOOKBACK_DAYS = 7
    integration = GscIntegrationFactory(last_synced_at=None)
    GscDailyTotalFactory(website=integration.website)
    start, end = ingestion_service.compute_sync_window(integration, today=TODAY)
    assert (end - start).days + 1 == 7


@pytest.mark.django_db
def test_prune_old_rows(settings):
    settings.GSC_RETENTION_DAYS = 30
    integration = GscIntegrationFactory()
    website = integration.website
    old_date = timezone.now().date() - timedelta(days=60)
    fresh_date = timezone.now().date() - timedelta(days=5)
    GscDailyTotalFactory(website=website, date=old_date)
    GscDailyTotalFactory(website=website, date=fresh_date)

    deleted = ingestion_service.prune_old_rows(website)

    assert deleted == 1
    remaining = GscDailyTotal.objects.filter(website=website)
    assert remaining.count() == 1
    assert remaining.first().date == fresh_date
