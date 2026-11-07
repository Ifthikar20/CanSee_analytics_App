"""A replayed recording under the same idempotency key must not double-count.

Simulates the acks_late redelivery of an audit cell: the provider call is
re-run, record_usage fires again with the same key, and both the ledger
and the outbox collapse onto the original rows.
"""
import pytest
from django.test import override_settings

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.metering.models import PolarEventOutbox
from core.ai_tracking import record_usage


@pytest.mark.django_db
class TestIdempotency:
    KEY = "audit:a1:cell:2:claude:upstream"

    def _record(self, user, key, tokens=100):
        record_usage(
            module="llm_ranking",
            model_name="claude-haiku-4-5",
            input_tokens=tokens,
            output_tokens=tokens,
            user=user,
            idempotency_key=key,
        )

    @override_settings(POLAR_INGEST_MODE="celery")
    def test_same_key_collapses_to_one_row(self):
        user = UserFactory(plan="individual")
        self._record(user, self.KEY)
        self._record(user, self.KEY, tokens=999)  # replay with drifted numbers

        assert AITokenUsage.objects.count() == 1
        assert PolarEventOutbox.objects.count() == 1
        # First write wins.
        assert AITokenUsage.objects.get().input_tokens == 100

    @override_settings(POLAR_INGEST_MODE="celery")
    def test_distinct_roles_in_a_cell_are_distinct_rows(self):
        user = UserFactory(plan="individual")
        self._record(user, "audit:a1:cell:2:claude:upstream")
        self._record(user, "audit:a1:cell:2:claude:extraction")
        assert AITokenUsage.objects.count() == 2
        assert PolarEventOutbox.objects.count() == 2

    def test_no_key_means_no_dedupe(self):
        user = UserFactory(plan="individual")
        self._record(user, None)
        self._record(user, None)
        assert AITokenUsage.objects.count() == 2
