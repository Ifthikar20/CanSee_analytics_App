"""Session bootstrap: the limits block the frontend hydrates plan gates from."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from core.utils.constants import Plan, SubscriptionStatus

SESSION_URL = "/api/v1/auth/session/"


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.mark.django_db
class TestSessionLimits:
    def test_free_user_gets_one_project(self, auth_client, settings):
        settings.PAYWALL_ENABLED = True
        client, user = auth_client
        data = client.get(SESSION_URL).json()["data"]
        assert data["limits"] == {"projects": 1}
        assert data["subscription"]["plan"] == "free"

    def test_trialing_pro_capped_at_free_allowance(self, auth_client, settings):
        settings.PAYWALL_ENABLED = True
        client, user = auth_client
        Subscription.objects.create(
            user=user,
            plan=Plan.PRO,
            status=SubscriptionStatus.TRIALING,
            polar_subscription_id="polar_sub_1",
            current_period_end=timezone.now() + timedelta(days=5),
        )
        data = client.get(SESSION_URL).json()["data"]
        assert data["subscription"]["is_trialing"] is True
        assert data["limits"] == {"projects": 1}

    def test_active_pro_gets_five(self, auth_client, settings):
        settings.PAYWALL_ENABLED = True
        client, user = auth_client
        Subscription.objects.create(
            user=user,
            plan=Plan.PRO,
            status=SubscriptionStatus.ACTIVE,
            polar_subscription_id="polar_sub_1",
            current_period_end=timezone.now() + timedelta(days=30),
        )
        data = client.get(SESSION_URL).json()["data"]
        assert data["limits"] == {"projects": 5}

    def test_paywall_off_unlimited(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        data = client.get(SESSION_URL).json()["data"]
        assert data["limits"] == {"projects": -1}
