"""record_usage() writes the ledger row and the Polar outbox row together.

Test settings force POLAR_INGEST_MODE=off; tests that exercise the outbox
opt in via override_settings, mirroring how dev/prod enable it.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.metering.models import PolarEventOutbox
from core.ai_tracking import record_usage


@pytest.mark.django_db
class TestOutboxWrite:
    def _record(self, user, **kw):
        defaults = dict(
            module="llm_ranking",
            model_name="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=50,
            user=user,
        )
        defaults.update(kw)
        record_usage(**defaults)

    @override_settings(POLAR_INGEST_MODE="celery")
    def test_ledger_and_outbox_written_together(self):
        user = UserFactory(plan="individual")
        self._record(user, metadata={"role": "upstream", "audit_id": "a1"})

        row = AITokenUsage.objects.get()
        event = PolarEventOutbox.objects.get()
        assert event.usage_id == row.id
        assert event.idempotency_key == row.idempotency_key
        assert event.external_customer_id == str(user.id)
        assert event.status == PolarEventOutbox.STATUS_PENDING
        payload = event.payload
        assert payload["name"] == "llm_usage"
        assert payload["external_id"] == row.idempotency_key
        assert payload["metadata"]["total_tokens"] == 150
        assert payload["metadata"]["module"] == "llm_ranking"
        assert payload["metadata"]["audit_id"] == "a1"
        # micro-USD is exact against the ledger's 6-decimal USD column
        assert payload["metadata"]["cost_micros"] == int(
            round(float(row.estimated_cost_usd) * 1_000_000)
        )

    def test_mode_off_writes_no_outbox(self):
        user = UserFactory(plan="individual")
        self._record(user)
        assert AITokenUsage.objects.count() == 1
        assert PolarEventOutbox.objects.count() == 0

    @override_settings(POLAR_INGEST_MODE="celery")
    def test_unattributed_usage_stays_local(self):
        # Pre-signup onboarding scans record user=None: ledger only.
        self._record(None, module="onboarding")
        assert AITokenUsage.objects.count() == 1
        assert PolarEventOutbox.objects.count() == 0

    @override_settings(POLAR_INGEST_MODE="celery")
    def test_ledger_survives_outbox_failure(self):
        user = UserFactory(plan="individual")
        with patch(
            "apps.metering.services.events.enqueue_usage_event",
            side_effect=RuntimeError("outbox exploded"),
        ):
            self._record(user)
        assert AITokenUsage.objects.count() == 1
        assert PolarEventOutbox.objects.count() == 0

    def test_cost_recorded_on_ledger(self):
        user = UserFactory(plan="individual")
        self._record(user, model_name="claude-haiku-4-5",
                     input_tokens=1_000_000, output_tokens=0)
        row = AITokenUsage.objects.get()
        assert row.estimated_cost_usd == Decimal("0.800000")
