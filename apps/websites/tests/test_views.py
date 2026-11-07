"""Create-project endpoint: URL normalization, duplicate guard, plan limits."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.websites.models import Website
from apps.websites.tests.factories import WebsiteFactory
from core.utils.constants import Plan, SubscriptionStatus

CREATE_URL = "/api/v1/websites/"


@pytest.fixture
def auth_client():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _payload(**overrides):
    payload = {
        "url": "strix.ai",
        "name": "strix",
        "industry": "cyber",
        "platform_type": "custom",
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
class TestCreateWebsite:
    def test_scheme_less_url_is_normalized(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["url"] == "https://strix.ai"
        assert data["name"] == "strix"
        assert Website.objects.filter(user=user, url="https://strix.ai").exists()

    def test_private_host_rejected_with_field_error(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        resp = client.post(CREATE_URL, _payload(url="localhost:8000"), format="json")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "validation_error"
        assert "url" in body["error"]["fields"]
        # The toast message is the concrete field error, not the old
        # generic "check your input" text.
        assert "publicly reachable" in body["error"]["message"]

    def test_missing_name_surfaces_field_error(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        resp = client.post(CREATE_URL, {"url": "strix.ai"}, format="json")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "validation_error"
        assert "name" in body["error"]["fields"]

    def test_duplicate_url_is_clean_400(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        WebsiteFactory(user=user, url="https://strix.ai")
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "duplicate_website"

    def test_soft_deleted_website_is_restored(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        site = WebsiteFactory(user=user, url="https://strix.ai")
        site.soft_delete(user=user)
        resp = client.post(CREATE_URL, _payload(name="strix reborn"), format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["id"] == str(site.id)
        site.refresh_from_db()
        assert site.is_deleted is False
        assert site.name == "strix reborn"

    def test_unauthenticated_401(self, db):
        resp = APIClient().post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 401


def _subscription(user, *, plan, status, polar_id="polar_sub_1", trial_days_left=5):
    return Subscription.objects.create(
        user=user,
        plan=plan,
        status=status,
        polar_subscription_id=polar_id,
        current_period_end=timezone.now() + timedelta(days=trial_days_left),
    )


@pytest.mark.django_db
class TestProjectLimit:
    """Free and trialing accounts get 1 project; paid Pro 5; Business unlimited."""

    @pytest.fixture(autouse=True)
    def _paywall_on(self, settings):
        settings.PAYWALL_ENABLED = True

    def test_free_user_capped_at_one(self, auth_client):
        client, user = auth_client
        WebsiteFactory(user=user)
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "project_limit_reached"
        assert body["error"]["meta"] == {"current": 1, "limit": 1}

    def test_free_user_first_project_allowed(self, auth_client):
        client, user = auth_client
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 201

    def test_trialing_pro_capped_at_one(self, auth_client):
        client, user = auth_client
        _subscription(user, plan=Plan.PRO, status=SubscriptionStatus.TRIALING)
        WebsiteFactory(user=user)
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "project_limit_reached"
        assert "trial" in body["error"]["message"].lower()

    def test_active_pro_gets_five(self, auth_client):
        client, user = auth_client
        _subscription(user, plan=Plan.PRO, status=SubscriptionStatus.ACTIVE)
        for _ in range(4):
            WebsiteFactory(user=user)
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 201

        resp = client.post(CREATE_URL, _payload(url="six.example.com"), format="json")
        assert resp.status_code == 403
        assert resp.json()["error"]["meta"]["limit"] == 5

    def test_business_unlimited(self, auth_client):
        client, user = auth_client
        _subscription(user, plan=Plan.BUSINESS, status=SubscriptionStatus.ACTIVE)
        for _ in range(6):
            WebsiteFactory(user=user)
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 201

    def test_paywall_off_is_unlimited(self, auth_client, settings):
        settings.PAYWALL_ENABLED = False
        client, user = auth_client
        for _ in range(6):
            WebsiteFactory(user=user)
        resp = client.post(CREATE_URL, _payload(), format="json")
        assert resp.status_code == 201


@pytest.mark.django_db
class TestUpdateWebsite:
    def test_blank_industry_is_accepted(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user, industry="")
        resp = client.put(
            f"{CREATE_URL}{website.id}/",
            {"name": "Renamed", "industry": "", "description": "New copy"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        website.refresh_from_db()
        assert website.name == "Renamed"
        assert website.description == "New copy"
        assert website.industry == ""

    def test_industry_can_be_cleared(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user, industry="fintech")
        resp = client.put(
            f"{CREATE_URL}{website.id}/", {"industry": ""}, format="json",
        )
        assert resp.status_code == 200, resp.content
        website.refresh_from_db()
        assert website.industry == ""

    def test_industry_length_still_validated(self, auth_client):
        client, user = auth_client
        website = WebsiteFactory(user=user)
        resp = client.put(
            f"{CREATE_URL}{website.id}/", {"industry": "x" * 101}, format="json",
        )
        assert resp.status_code == 400
