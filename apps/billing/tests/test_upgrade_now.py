"""Upgrade-now (end trial early) and monthly<->annual cycle switch.

All polar_client calls are mocked — no network. The sync path runs for
real so the local row mirrors what the mocked Polar state reports.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.billing.services import polar_billing
from apps.billing.tests.test_polar_billing import (
    ANNUAL_ID,
    MONTHLY_ID,
    _state,
    polar_configured,
)
from apps.metering import polar_client
from core.utils.constants import Plan, SubscriptionStatus

UPGRADE_URL = "/api/v1/billing/upgrade-now/"
CHANGE_URL = "/api/v1/billing/change-plan/"


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _sub_row(user, *, status=SubscriptionStatus.TRIALING, interval="month"):
    now = timezone.now()
    return Subscription.objects.create(
        user=user, plan=Plan.PRO, status=status,
        polar_subscription_id="ps_123", interval=interval,
        current_period_start=now - timedelta(days=2),
        current_period_end=now + timedelta(days=5),
    )


def _polar_sub(status="active", product_id=MONTHLY_ID, interval="month"):
    now = timezone.now()
    return SimpleNamespace(
        id="ps_123",
        status=status,
        product_id=product_id,
        recurring_interval=interval,
        current_period_start=now,
        current_period_end=now + timedelta(days=30 if interval == "month" else 365),
        cancel_at_period_end=False,
        started_at=now - timedelta(days=2),
    )


@pytest.mark.django_db
class TestUpgradeNow:
    @polar_configured
    def test_trialing_upgrade_charges_now_and_unlocks_limits(self, settings):
        settings.PAYWALL_ENABLED = True
        user = UserFactory()
        _sub_row(user)
        with patch.object(polar_client, "update_subscription_plan") as mock_update, \
             patch.object(polar_client, "get_customer_state",
                          return_value=_state([_polar_sub()])):
            resp = _client(user).post(UPGRADE_URL, {}, format="json")

        assert resp.status_code == 200
        mock_update.assert_called_once_with(
            "ps_123", end_trial_now=True, product_id=None,
        )
        data = resp.json()["data"]["data"]
        assert data["subscription"]["status"] == "active"
        assert data["subscription"]["is_trialing"] is False
        # The whole point: the paid allowance is live immediately.
        assert data["limits"]["projects"] == 5
        row = Subscription.objects.get(user=user)
        assert row.status == SubscriptionStatus.ACTIVE
        assert row.interval == "month"

    @polar_configured
    def test_upgrade_can_switch_to_annual_in_the_same_call(self):
        user = UserFactory()
        _sub_row(user, interval="month")
        with patch.object(polar_client, "update_subscription_plan") as mock_update, \
             patch.object(polar_client, "get_customer_state",
                          return_value=_state([_polar_sub(product_id=ANNUAL_ID,
                                                          interval="year")])):
            resp = _client(user).post(UPGRADE_URL, {"annual": True}, format="json")

        assert resp.status_code == 200
        mock_update.assert_called_once_with(
            "ps_123", end_trial_now=True, product_id=ANNUAL_ID,
        )
        assert Subscription.objects.get(user=user).interval == "year"

    @polar_configured
    def test_active_subscription_cannot_use_upgrade_now(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.ACTIVE)
        resp = _client(user).post(UPGRADE_URL, {}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "not_trialing"

    @polar_configured
    def test_no_subscription_is_a_clean_400(self):
        user = UserFactory()
        resp = _client(user).post(UPGRADE_URL, {}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_subscription"

    @polar_configured
    def test_polar_failure_maps_to_billing_failed(self):
        user = UserFactory()
        _sub_row(user)
        with patch.object(polar_client, "update_subscription_plan",
                          side_effect=polar_client.PolarUnavailable("down")):
            resp = _client(user).post(UPGRADE_URL, {}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "billing_failed"
        # The local row must not have been promoted.
        assert Subscription.objects.get(user=user).status == SubscriptionStatus.TRIALING


@pytest.mark.django_db
class TestChangeBillingCycle:
    @polar_configured
    def test_active_monthly_to_annual_settles_immediately(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.ACTIVE, interval="month")
        with patch.object(polar_client, "update_subscription_plan") as mock_update, \
             patch.object(polar_client, "get_customer_state",
                          return_value=_state([_polar_sub(product_id=ANNUAL_ID,
                                                          interval="year")])):
            resp = _client(user).post(CHANGE_URL, {"annual": True}, format="json")

        assert resp.status_code == 200
        mock_update.assert_called_once_with(
            "ps_123", product_id=ANNUAL_ID, proration_behavior="invoice",
        )
        assert resp.json()["data"]["data"]["subscription"]["interval"] == "year"

    @polar_configured
    def test_trialing_switch_has_no_proration(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.TRIALING, interval="month")
        with patch.object(polar_client, "update_subscription_plan") as mock_update, \
             patch.object(polar_client, "get_customer_state",
                          return_value=_state([_polar_sub(status="trialing",
                                                          product_id=ANNUAL_ID,
                                                          interval="year")])):
            resp = _client(user).post(CHANGE_URL, {"annual": True}, format="json")

        assert resp.status_code == 200
        mock_update.assert_called_once_with(
            "ps_123", product_id=ANNUAL_ID, proration_behavior=None,
        )

    @polar_configured
    def test_same_cycle_is_rejected(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.ACTIVE, interval="year")
        resp = _client(user).post(CHANGE_URL, {"annual": True}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "already_on_plan"

    @polar_configured
    def test_missing_annual_flag_is_rejected(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.ACTIVE)
        resp = _client(user).post(CHANGE_URL, {}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    @polar_configured
    def test_canceled_subscription_cannot_switch(self):
        user = UserFactory()
        _sub_row(user, status=SubscriptionStatus.CANCELED)
        resp = _client(user).post(CHANGE_URL, {"annual": True}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "not_active"


@pytest.mark.django_db
class TestIntervalPlumbing:
    @polar_configured
    def test_sync_copies_recurring_interval(self):
        user = UserFactory()
        with patch.object(polar_client, "get_customer_state",
                          return_value=_state([_polar_sub(interval="year")])):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.interval == "year"

    def test_subscription_state_exposes_interval(self):
        from apps.billing.services.plan_limits import subscription_state

        row = Subscription(
            plan=Plan.PRO, status=SubscriptionStatus.ACTIVE,
            polar_subscription_id="ps_1", interval="year",
        )
        assert subscription_state(row)["interval"] == "year"
        assert subscription_state(None)["interval"] is None
