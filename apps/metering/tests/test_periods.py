"""billing_period_for(): subscription cycle when live, calendar month else."""
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.metering.services.periods import billing_period_for, month_bounds
from core.utils.constants import SubscriptionStatus


@pytest.mark.django_db
class TestBillingPeriod:
    @freeze_time("2026-08-19 12:00:00")
    def test_no_subscription_falls_back_to_calendar_month(self):
        user = UserFactory(plan="individual")
        period = billing_period_for(user)
        assert period.source == "calendar"
        assert period.start == datetime(2026, 8, 1, tzinfo=UTC)
        assert period.end == datetime(2026, 9, 1, tzinfo=UTC)
        assert period.key == "2026-08-01"

    @freeze_time("2026-12-19 12:00:00")
    def test_calendar_month_december_rollover(self):
        start, end = month_bounds()
        assert start == datetime(2026, 12, 1, tzinfo=UTC)
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    @freeze_time("2026-08-19 12:00:00")
    def test_active_subscription_period_wins(self):
        user = UserFactory(plan="individual")
        Subscription.objects.create(
            user=user,
            plan="individual",
            status=SubscriptionStatus.ACTIVE,
            current_period_start=datetime(2026, 8, 10, tzinfo=UTC),
            current_period_end=datetime(2026, 9, 10, tzinfo=UTC),
        )
        period = billing_period_for(user)
        assert period.source == "subscription"
        assert period.start == datetime(2026, 8, 10, tzinfo=UTC)
        assert period.end == datetime(2026, 9, 10, tzinfo=UTC)

    @freeze_time("2026-08-19 12:00:00")
    def test_stale_subscription_period_falls_back(self):
        # Period bounds that do not contain "now" (e.g. a sub that stopped
        # syncing) must not define the usage window.
        user = UserFactory(plan="individual")
        Subscription.objects.create(
            user=user,
            plan="individual",
            status=SubscriptionStatus.ACTIVE,
            current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
        assert billing_period_for(user).source == "calendar"

    @freeze_time("2026-08-19 12:00:00")
    def test_canceled_subscription_falls_back(self):
        user = UserFactory(plan="individual")
        Subscription.objects.create(
            user=user,
            plan="individual",
            status=SubscriptionStatus.CANCELED,
            current_period_start=datetime(2026, 8, 10, tzinfo=UTC),
            current_period_end=datetime(2026, 9, 10, tzinfo=UTC),
        )
        assert billing_period_for(user).source == "calendar"

    def test_none_user_gets_calendar_month(self):
        assert billing_period_for(None).source == "calendar"
