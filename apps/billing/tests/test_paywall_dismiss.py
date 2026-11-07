"""
Tests for POST /api/v1/billing/paywall/dismiss/ — the "Continue with the
free plan" action on the paywall — and its effect on SessionView routing.
"""
import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.websites.tests.factories import WebsiteFactory
from core.utils.constants import SubscriptionStatus

SESSION_URL = "/api/v1/auth/session/"
DISMISS_URL = "/api/v1/billing/paywall/dismiss/"


@pytest.fixture
def user(db):
    return UserFactory()


def _client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestPaywallDismissEndpoint:
    def test_requires_auth(self):
        assert _client().post(DISMISS_URL).status_code == 401

    def test_get_not_allowed(self, user):
        assert _client(user).get(DISMISS_URL).status_code == 405

    def test_free_user_dismisses(self, user):
        resp = _client(user).post(DISMISS_URL)
        assert resp.status_code == 200
        assert resp.data["paywall_dismissed"] is True
        assert resp.data["already_paying"] is False
        assert resp.data["dismissed_at"]
        user.refresh_from_db()
        assert user.paywall_dismissed_at is not None

    def test_idempotent_double_post_keeps_first_timestamp(self, user):
        client = _client(user)
        client.post(DISMISS_URL)
        user.refresh_from_db()
        first = user.paywall_dismissed_at
        resp = client.post(DISMISS_URL)
        assert resp.status_code == 200
        assert resp.data["paywall_dismissed"] is True
        user.refresh_from_db()
        assert user.paywall_dismissed_at == first

    def test_paying_user_is_noop(self, user):
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.ACTIVE, plan="pro",
        )
        resp = _client(user).post(DISMISS_URL)
        assert resp.status_code == 200
        assert resp.data["already_paying"] is True
        assert resp.data["paywall_dismissed"] is False
        user.refresh_from_db()
        assert user.paywall_dismissed_at is None


@pytest.mark.django_db
class TestDismissRouting:
    def test_dismiss_flips_next_route_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        client = _client(user)

        assert client.get(SESSION_URL).data["next_route"] == "paywall"

        assert client.post(DISMISS_URL).status_code == 200

        data = client.get(SESSION_URL).data
        assert data["next_route"] == "app"
        assert data["paywall_dismissed"] is True

    def test_dismissed_user_can_still_subscribe(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        client = _client(user)
        client.post(DISMISS_URL)

        Subscription.objects.create(
            user=user, status=SubscriptionStatus.ACTIVE, plan="pro",
        )
        # Authenticate a freshly loaded instance: the dismiss call above
        # cached the "no subscription" reverse-relation miss on `user`;
        # a real follow-up request starts from a clean instance.
        from django.contrib.auth import get_user_model
        fresh = get_user_model().objects.get(pk=user.pk)
        data = _client(fresh).get(SESSION_URL).data
        assert data["next_route"] == "app"
        assert data["subscription"]["is_paying"] is True
        assert data["paywall_dismissed"] is True
