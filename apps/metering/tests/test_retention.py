"""Retention tasks: ledger pruning is gated on Polar being authoritative."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.metering.models import PolarEventOutbox
from apps.metering.tasks import prune_ai_usage_ledger, prune_polar_outbox


def _aged_row(user, days_old):
    row = AITokenUsage.objects.create(
        user=user, module="llm_ranking", provider="anthropic",
        model_name="claude-haiku-4-5", input_tokens=1, output_tokens=1,
        total_tokens=2, estimated_cost_usd=Decimal("0.01"),
    )
    AITokenUsage.objects.filter(id=row.id).update(
        created_at=timezone.now() - timedelta(days=days_old)
    )
    return row


@pytest.mark.django_db
class TestLedgerPruning:
    def test_gated_off_until_polar_is_authoritative(self):
        user = UserFactory(plan="individual")
        _aged_row(user, 500)
        result = prune_ai_usage_ledger()
        assert result == {"deleted": 0}
        assert AITokenUsage.objects.count() == 1

    @override_settings(POLAR_READS_ENABLED=True, AI_USAGE_RETENTION_DAYS=400)
    def test_prunes_only_rows_past_retention(self):
        user = UserFactory(plan="individual")
        _aged_row(user, 500)
        keep = _aged_row(user, 100)
        result = prune_ai_usage_ledger()
        assert result["deleted"] == 1
        assert list(AITokenUsage.objects.values_list("id", flat=True)) == [keep.id]


@pytest.mark.django_db
class TestOutboxPruning:
    def test_sent_and_dead_rows_age_out(self):
        old_sent = PolarEventOutbox.objects.create(
            idempotency_key="s1", external_customer_id="x",
            status=PolarEventOutbox.STATUS_SENT,
            sent_at=timezone.now() - timedelta(days=40),
        )
        fresh_sent = PolarEventOutbox.objects.create(
            idempotency_key="s2", external_customer_id="x",
            status=PolarEventOutbox.STATUS_SENT,
            sent_at=timezone.now() - timedelta(days=5),
        )
        pending = PolarEventOutbox.objects.create(
            idempotency_key="p1", external_customer_id="x",
        )

        result = prune_polar_outbox()
        assert result["sent_deleted"] == 1
        remaining = set(
            PolarEventOutbox.objects.values_list("idempotency_key", flat=True)
        )
        assert remaining == {fresh_sent.idempotency_key, pending.idempotency_key}
        assert old_sent.idempotency_key not in remaining
