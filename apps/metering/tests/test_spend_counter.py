"""Hot-path spend counter: O(1) steady state, threshold crossings fire once."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.metering.services import spend_counter
from core.utils.constants import SubscriptionStatus


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _pro_user():
    """User on an ACTIVE Pro subscription (cap $29.25)."""
    user = UserFactory(plan="pro")
    now = timezone.now()
    Subscription.objects.create(
        user=user, plan="pro",
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
    )
    return user


def _ledger(user, usd):
    AITokenUsage.objects.create(
        user=user, module="llm_ranking", provider="anthropic",
        model_name="claude-haiku-4-5", input_tokens=1, output_tokens=1,
        total_tokens=2, estimated_cost_usd=Decimal(str(usd)),
    )


@pytest.mark.django_db
class TestSpendCounter:
    def test_crossing_warn_threshold_dispatches_once(self):
        # Individual cap = $29.25; warn at 80% = $23.40.
        user = _pro_user()
        _ledger(user, 23.0)  # existing period spend, seeds the counter

        with patch.object(spend_counter, "_dispatch_notify") as mock_notify:
            spend_counter.bump(user, 0.50)  # 23.00 -> 23.50 crosses 23.40
            spend_counter.bump(user, 0.10)  # already past; no re-fire
        assert mock_notify.call_count == 1
        assert mock_notify.call_args.args[1] == "ai_cap_warning"

    def test_crossing_hard_cap_prefers_exceeded(self):
        user = _pro_user()
        _ledger(user, 29.0)
        with patch.object(spend_counter, "_dispatch_notify") as mock_notify:
            spend_counter.bump(user, 0.50)  # 29.00 -> 29.50 crosses 29.25
        assert mock_notify.call_count == 1
        assert mock_notify.call_args.args[1] == "ai_cap_exceeded"

    def test_steady_state_runs_no_aggregate_queries(self, django_assert_max_num_queries):
        # After the first bump seeds the Redis counter and the cap cache,
        # the hot path may only touch the subscription-period lookup —
        # never the two full-ledger aggregates the old code ran per call.
        user = _pro_user()
        spend_counter.bump(user, 0.01)  # seeds counter + cap cache
        with django_assert_max_num_queries(1):
            spend_counter.bump(user, 0.01)

    def test_no_cap_no_counter(self):
        user = UserFactory(plan="individual")
        with patch.object(spend_counter, "effective_cap_cached", return_value=0.0), \
             patch.object(spend_counter, "_dispatch_notify") as mock_notify:
            spend_counter.bump(user, 5.0)
        assert mock_notify.call_count == 0


@pytest.mark.django_db
class TestNotifyTask:
    def test_task_reverifies_against_ledger(self):
        # Counter drift scenario: task fires but the ledger says the
        # threshold has NOT been crossed -> no notification.
        from apps.metering.tasks import notify_cap_threshold

        user = _pro_user()
        _ledger(user, 1.0)
        notify_cap_threshold(str(user.id), "ai_cap_warning")

        from apps.notifications.models import Notification

        assert Notification.objects.filter(user=user).count() == 0

    def test_task_creates_notification_once_per_period(self):
        from apps.metering.tasks import notify_cap_threshold

        user = _pro_user()
        _ledger(user, 29.30)  # past the $29.25 cap
        notify_cap_threshold(str(user.id), "ai_cap_exceeded")
        notify_cap_threshold(str(user.id), "ai_cap_exceeded")

        from apps.notifications.models import Notification

        rows = Notification.objects.filter(user=user, type="ai_cap_exceeded")
        assert rows.count() == 1
        assert "allowance" in rows.first().title.lower()
