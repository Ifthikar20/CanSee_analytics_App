"""Outbox flush: delivery, retry/backoff, poison isolation, dead-lettering.

The Polar client wrapper is mocked at the module boundary
(apps.metering.tasks imports polar_client functions via the package), so
no test touches the network.
"""
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.metering import polar_client
from apps.metering.models import PolarCustomer, PolarEventOutbox
from apps.metering.tasks import flush_outbox_once


def _outbox_row(user, key, **kw):
    import uuid as _uuid

    defaults = dict(
        idempotency_key=key,
        # For the deleted-user case use a real (but nonexistent) UUID —
        # external_customer_id is always str(user.id) in production.
        external_customer_id=str(user.id) if user else str(_uuid.uuid4()),
        name="llm_usage",
        payload={"name": "llm_usage", "external_id": key, "metadata": {}},
    )
    defaults.update(kw)
    return PolarEventOutbox.objects.create(**defaults)


def _provision(user):
    PolarCustomer.objects.create(
        user=user, polar_customer_id="pc_1", environment="sandbox",
        synced_at=timezone.now(),
    )


@pytest.mark.django_db
class TestFlush:
    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_success_marks_sent(self):
        user = UserFactory(plan="individual")
        _provision(user)
        _outbox_row(user, "k1")
        _outbox_row(user, "k2")

        with patch.object(polar_client, "ingest", return_value=(2, 0)) as mock_ingest:
            result = flush_outbox_once()

        assert result["sent"] == 2
        assert mock_ingest.call_count == 1
        assert not PolarEventOutbox.objects.exclude(
            status=PolarEventOutbox.STATUS_SENT
        ).exists()
        sent = PolarEventOutbox.objects.first()
        assert sent.sent_at is not None

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_unavailable_backs_off_and_retries_later(self):
        user = UserFactory(plan="individual")
        _provision(user)
        row = _outbox_row(user, "k1")

        with patch.object(
            polar_client, "ingest", side_effect=polar_client.PolarUnavailable("down")
        ):
            flush_outbox_once()

        row.refresh_from_db()
        assert row.status == PolarEventOutbox.STATUS_PENDING
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        assert row.next_attempt_at > timezone.now()
        assert "down" in row.last_error

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_rejected_single_event_goes_dead(self):
        user = UserFactory(plan="individual")
        _provision(user)
        row = _outbox_row(user, "k1")

        with patch.object(
            polar_client, "ingest", side_effect=polar_client.PolarRejected("HTTP 422")
        ):
            flush_outbox_once()

        row.refresh_from_db()
        assert row.status == PolarEventOutbox.STATUS_DEAD

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_rejected_batch_bisects_to_isolate_poison(self):
        user = UserFactory(plan="individual")
        _provision(user)
        good1 = _outbox_row(user, "good1")
        poison = _outbox_row(user, "poison")
        good2 = _outbox_row(user, "good2")

        def fake_ingest(events):
            if any(e["external_id"] == "poison" for e in events):
                raise polar_client.PolarRejected("HTTP 422")
            return (len(events), 0)

        with patch.object(polar_client, "ingest", side_effect=fake_ingest):
            flush_outbox_once()

        good1.refresh_from_db()
        poison.refresh_from_db()
        good2.refresh_from_db()
        assert good1.status == PolarEventOutbox.STATUS_SENT
        assert good2.status == PolarEventOutbox.STATUS_SENT
        assert poison.status == PolarEventOutbox.STATUS_DEAD

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_max_attempts_dead_letters(self):
        user = UserFactory(plan="individual")
        _provision(user)
        row = _outbox_row(user, "k1", attempts=PolarEventOutbox.MAX_ATTEMPTS - 1)

        with patch.object(
            polar_client, "ingest", side_effect=polar_client.PolarUnavailable("down")
        ):
            flush_outbox_once()

        row.refresh_from_db()
        assert row.status == PolarEventOutbox.STATUS_DEAD

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_deleted_user_rows_go_dead_without_send(self):
        _outbox_row(None, "orphan")

        with patch.object(polar_client, "ingest") as mock_ingest:
            flush_outbox_once()

        assert mock_ingest.call_count == 0
        row = PolarEventOutbox.objects.get()
        assert row.status == PolarEventOutbox.STATUS_DEAD
        assert "user no longer exists" in row.last_error

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_rejected_customer_dead_letters_their_rows(self):
        # Polar refuses some customers permanently (e.g. undeliverable
        # email domains). Their rows must dead-letter with the create
        # error, not retry forever.
        user = UserFactory(plan="individual")  # deliberately NOT provisioned
        row = _outbox_row(user, "k1")

        with patch.object(
            polar_client,
            "ensure_customer",
            side_effect=polar_client.PolarRejected("HTTP 422: email undeliverable"),
        ), patch.object(polar_client, "ingest") as mock_ingest:
            result = flush_outbox_once()

        assert result["sent"] == 0
        assert mock_ingest.call_count == 0
        row.refresh_from_db()
        assert row.status == PolarEventOutbox.STATUS_DEAD
        assert "customer rejected" in row.last_error
        assert "undeliverable" in row.last_error

    @override_settings(POLAR_ENVIRONMENT="sandbox")
    def test_future_next_attempt_rows_are_skipped(self):
        user = UserFactory(plan="individual")
        _provision(user)
        _outbox_row(
            user, "k1",
            next_attempt_at=timezone.now() + timezone.timedelta(hours=1),
        )
        with patch.object(polar_client, "ingest") as mock_ingest:
            result = flush_outbox_once()
        assert result["sent"] == 0
        assert mock_ingest.call_count == 0
