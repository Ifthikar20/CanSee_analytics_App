"""Tests for Search Console Celery tasks."""

from unittest.mock import patch

import pytest

from apps.search_console import tasks
from apps.search_console.tests.factories import GscIntegrationFactory


@pytest.mark.django_db
def test_sync_all_gsc_dispatches_only_ready_integrations():
    ready = GscIntegrationFactory()
    GscIntegrationFactory(is_active=False)  # inactive -> skipped
    GscIntegrationFactory(metadata={})      # no site_url -> skipped

    with patch.object(tasks.sync_website_gsc, "delay") as mock_delay:
        dispatched = tasks.sync_all_gsc()

    assert dispatched == 1
    mock_delay.assert_called_once_with(integration_id=ready.pk)


@pytest.mark.django_db
def test_sync_website_gsc_chains_sync_prune_and_feed():
    integration = GscIntegrationFactory()

    with patch(
        "apps.search_console.services.ingestion_service.sync_window",
        return_value={"totals": 2, "queries": 1, "pages": 1},
    ) as mock_sync, patch(
        "apps.search_console.services.ingestion_service.prune_old_rows",
        return_value=0,
    ) as mock_prune, patch(
        "apps.search_console.services.prompt_feed.feed_prompts_for_website",
        return_value={"created": 1, "boosted": 0, "skipped": 0},
    ) as mock_feed:
        result = tasks.sync_website_gsc.apply(
            kwargs={"integration_id": integration.pk}
        ).get()

    mock_sync.assert_called_once()
    mock_prune.assert_called_once()
    mock_feed.assert_called_once()
    assert result["totals"] == 2
    assert result["prompt_feed"]["created"] == 1


@pytest.mark.django_db
def test_sync_website_gsc_feed_failure_does_not_fail_sync():
    integration = GscIntegrationFactory()
    with patch(
        "apps.search_console.services.ingestion_service.sync_window",
        return_value={"totals": 1},
    ), patch(
        "apps.search_console.services.ingestion_service.prune_old_rows",
        return_value=0,
    ), patch(
        "apps.search_console.services.prompt_feed.feed_prompts_for_website",
        side_effect=RuntimeError("boom"),
    ):
        result = tasks.sync_website_gsc.apply(
            kwargs={"integration_id": integration.pk}
        ).get()

    assert result["totals"] == 1
    assert "error" in result["prompt_feed"]


@pytest.mark.django_db
def test_sync_website_gsc_missing_integration_is_skipped():
    result = tasks.sync_website_gsc.apply(
        kwargs={"integration_id": 999999}
    ).get()
    assert result == {"skipped": "not_found"}


@pytest.mark.django_db
def test_sync_website_gsc_backfill_days_window():
    integration = GscIntegrationFactory()
    captured = {}

    def fake_sync(integ, *, start_date, end_date):
        captured["start"] = start_date
        captured["end"] = end_date
        return {}

    with patch(
        "apps.search_console.services.ingestion_service.sync_window",
        side_effect=fake_sync,
    ), patch(
        "apps.search_console.services.ingestion_service.prune_old_rows",
        return_value=0,
    ), patch(
        "apps.search_console.services.prompt_feed.feed_prompts_for_website",
        return_value={},
    ):
        tasks.sync_website_gsc.apply(
            kwargs={"integration_id": integration.pk, "backfill_days": 28}
        ).get()

    assert (captured["end"] - captured["start"]).days + 1 == 28
