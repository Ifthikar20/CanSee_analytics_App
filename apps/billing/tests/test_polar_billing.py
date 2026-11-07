"""Polar billing: checkout, confirmation, state sync, webhook.

All polar_client functions are mocked — no network. The key invariant:
sync_from_customer_state is the single writer, and populating the
subscription period flips the metering usage window to the real billing
cycle (the Pass 1 seam).
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import BillingEvent, Subscription
from apps.billing.services import polar_billing
from apps.metering import polar_client
from core.utils.constants import Plan, SubscriptionStatus

MONTHLY_ID = "11111111-1111-1111-1111-111111111111"
ANNUAL_ID = "22222222-2222-2222-2222-222222222222"

polar_configured = override_settings(
    POLAR_ACCESS_TOKEN="token",
    POLAR_PRODUCT_PRO_MONTHLY_ID=MONTHLY_ID,
    POLAR_PRODUCT_PRO_ANNUAL_ID=ANNUAL_ID,
)


def _polar_sub(product_id=MONTHLY_ID, status="active", cancel=False):
    now = timezone.now()
    return SimpleNamespace(
        id="ps_123",
        status=status,
        product_id=product_id,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        cancel_at_period_end=cancel,
        started_at=now - timedelta(days=1),
    )


def _state(subs):
    return SimpleNamespace(active_subscriptions=subs)


@pytest.mark.django_db
class TestSyncFromCustomerState:
    @polar_configured
    def test_active_subscription_maps_to_pro(self):
        user = UserFactory(plan="starter")
        with patch.object(
            polar_client, "get_customer_state", return_value=_state([_polar_sub()])
        ):
            sub = polar_billing.sync_from_customer_state(user)

        assert sub.plan == Plan.PRO
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.polar_subscription_id == "ps_123"
        assert sub.current_period_start is not None
        user.refresh_from_db()
        assert user.plan == Plan.PRO

        # The Pass 1 seam: a populated period flips the usage window
        # from calendar month to the real billing cycle.
        from apps.metering.services.periods import billing_period_for

        assert billing_period_for(user).source == "subscription"

    @polar_configured
    def test_trialing_subscription_maps_to_trialing(self):
        user = UserFactory(plan="starter")
        with patch.object(
            polar_client,
            "get_customer_state",
            return_value=_state([_polar_sub(status="trialing")]),
        ):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.status == SubscriptionStatus.TRIALING

    @polar_configured
    def test_empty_state_never_grants_access(self):
        # A Polar customer with no subscriptions (metering creates
        # customers for everyone) must not leave behind a fresh row on
        # the model's TRIALING default — that would leak paid feature
        # gates through current_plan_for.
        from apps.billing.services.plan_limits import current_plan_for, is_paying

        user = UserFactory(plan="starter")
        with patch.object(
            polar_client, "get_customer_state", return_value=_state([])
        ):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.status == SubscriptionStatus.CANCELED
        assert is_paying(user) is False
        user.refresh_from_db()
        assert current_plan_for(user) == Plan.FREE

    @polar_configured
    def test_enum_status_is_unwrapped(self):
        # The real SDK returns enum statuses whose str() is
        # "Status.TRIALING" — regression: trials rendered as active.
        import enum

        class Status(enum.Enum):
            TRIALING = "trialing"

        user = UserFactory(plan="starter")
        with patch.object(
            polar_client,
            "get_customer_state",
            return_value=_state([_polar_sub(status=Status.TRIALING)]),
        ):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.status == SubscriptionStatus.TRIALING

    @polar_configured
    def test_vanished_subscription_cancels_managed_row(self):
        user = UserFactory(plan="pro")
        Subscription.objects.create(
            user=user,
            polar_subscription_id="ps_123",
            plan=Plan.PRO,
            status=SubscriptionStatus.ACTIVE,
        )
        # Gone from customer state AND the by-id read 404s: truly gone.
        with (
            patch.object(polar_client, "get_customer_state", return_value=_state([])),
            patch.object(
                polar_client, "get_subscription",
                side_effect=polar_client.PolarRejected("404"),
            ),
        ):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.status == SubscriptionStatus.CANCELED

    @polar_configured
    def test_unknown_product_is_ignored(self):
        user = UserFactory(plan="starter")
        with patch.object(
            polar_client,
            "get_customer_state",
            return_value=_state([_polar_sub(product_id="99999999-0000-0000-0000-000000000000")]),
        ):
            sub = polar_billing.sync_from_customer_state(user)
        assert sub.polar_subscription_id is None


@pytest.mark.django_db
class TestConfirmCheckout:
    @polar_configured
    def test_succeeded_checkout_activates(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(
            id="co_1", status="succeeded", external_customer_id=str(user.id)
        )
        with patch.object(polar_client, "get_checkout", return_value=checkout), patch.object(
            polar_client, "get_customer_state", return_value=_state([_polar_sub()])
        ):
            result = polar_billing.confirm_checkout(user, "co_1")
        assert result["active"] is True
        assert result["plan"] == Plan.PRO

    @polar_configured
    def test_foreign_checkout_is_rejected(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(
            id="co_1", status="succeeded", external_customer_id="someone-else"
        )
        with patch.object(polar_client, "get_checkout", return_value=checkout):
            with pytest.raises(Exception) as exc_info:
                polar_billing.confirm_checkout(user, "co_1")
        assert "belong" in str(exc_info.value)

    @polar_configured
    def test_open_checkout_reports_inactive(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(
            id="co_1", status="open", external_customer_id=str(user.id)
        )
        with patch.object(polar_client, "get_checkout", return_value=checkout):
            result = polar_billing.confirm_checkout(user, "co_1")
        assert result == {"status": "open", "active": False}


@pytest.mark.django_db
class TestCheckoutView:
    @pytest.fixture(autouse=True)
    def _no_polar_customer(self, request):
        # create_checkout self-heals stale local state by consulting
        # Polar's customer state first. Default that to "no customer"
        # (PolarRejected) so the suite stays offline; tests that cover
        # the self-heal re-patch it themselves.
        if "no_customer_state_patch" in request.keywords:
            yield
            return
        with patch.object(
            polar_client, "get_customer_state",
            side_effect=polar_client.PolarRejected("no customer"),
        ):
            yield

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    @polar_configured
    @pytest.mark.no_customer_state_patch
    def test_checkout_self_heals_stale_local_state(self):
        # A paid checkout whose confirm redirect never landed leaves the
        # local DB unpaid while Polar has an active subscription — the
        # paywall then loops forever. The pre-checkout sync must repair
        # the row and refuse the duplicate checkout.
        user = UserFactory(plan="starter")
        with patch.object(
            polar_client, "get_customer_state",
            return_value=_state([_polar_sub(status="trialing")]),
        ), patch.object(polar_client, "create_checkout") as mock_create:
            resp = self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
            )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "already_subscribed"
        mock_create.assert_not_called()
        sub = Subscription.objects.get(user=user)
        assert sub.status == SubscriptionStatus.TRIALING
        assert sub.polar_subscription_id == "ps_123"

    @polar_configured
    def test_checkout_returns_polar_url(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://sandbox.polar.sh/checkout/co_1")
        with patch.object(polar_client, "create_checkout", return_value=checkout) as mock_create:
            resp = self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro", "annual": False}, format="json",
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["data"]["checkout_url"].startswith("https://sandbox.polar.sh")
        kwargs = mock_create.call_args.kwargs
        assert kwargs["product_ids"] == [MONTHLY_ID]
        assert kwargs["external_customer_id"] == str(user.id)
        assert "checkout_id={CHECKOUT_ID}" in kwargs["success_url"]

    @polar_configured
    def test_annual_uses_annual_product(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://x")
        with patch.object(polar_client, "create_checkout", return_value=checkout) as mock_create:
            self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro", "annual": True}, format="json",
            )
        assert mock_create.call_args.kwargs["product_ids"] == [ANNUAL_ID]

    @polar_configured
    def test_checkout_url_carries_light_theme(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://sandbox.polar.sh/checkout/co_1")
        with patch.object(polar_client, "create_checkout", return_value=checkout):
            resp = self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
            )
        assert resp.json()["data"]["data"]["checkout_url"].endswith("?theme=light")

    @polar_configured
    def test_hostile_origin_falls_back_to_frontend_url(self):
        # The success redirect is unsigned, so a spoofed Origin header
        # must never decide where a paying customer lands afterwards.
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://x")
        with patch.object(polar_client, "create_checkout", return_value=checkout) as mock_create:
            self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
                HTTP_ORIGIN="https://evil.example",
            )
        from django.conf import settings as dj_settings

        assert mock_create.call_args.kwargs["success_url"].startswith(
            dj_settings.FRONTEND_URL
        )

    @polar_configured
    def test_allowlisted_origin_is_honored(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://x")
        with override_settings(CORS_ALLOWED_ORIGINS=["https://app.cansee.ai"]), patch.object(
            polar_client, "create_checkout", return_value=checkout
        ) as mock_create:
            self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
                HTTP_ORIGIN="https://app.cansee.ai",
            )
        assert mock_create.call_args.kwargs["success_url"].startswith(
            "https://app.cansee.ai/billing?"
        )

    @polar_configured
    def test_client_ip_forwarded_for_tax_location(self):
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://x")
        with patch.object(polar_client, "create_checkout", return_value=checkout) as mock_create:
            self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1",
            )
        assert mock_create.call_args.kwargs["customer_ip_address"] == "203.0.113.9"

    @polar_configured
    def test_undeliverable_email_retries_without_prefill(self):
        # Polar 422s emails on mail-less domains (dev/test accounts).
        # The service must retry once without customer_email so checkout
        # still opens; identity stays bound via external_customer_id.
        user = UserFactory(plan="starter")
        checkout = SimpleNamespace(id="co_1", url="https://sandbox.polar.sh/checkout/co_1")
        reject = polar_client.PolarRejected(
            'HTTP 422: {"loc":["body","customer_email"],"msg":"not valid"}'
        )
        with patch.object(
            polar_client, "create_checkout", side_effect=[reject, checkout]
        ) as mock_create:
            resp = self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
            )
        assert resp.status_code == 200
        assert mock_create.call_count == 2
        assert mock_create.call_args_list[0].kwargs["customer_email"] == user.email
        assert mock_create.call_args_list[1].kwargs["customer_email"] is None

    @polar_configured
    def test_non_email_rejection_is_not_retried(self):
        user = UserFactory(plan="starter")
        reject = polar_client.PolarRejected("HTTP 422: something unrelated")
        with patch.object(
            polar_client, "create_checkout", side_effect=reject
        ) as mock_create:
            resp = self._client(user).post(
                "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
            )
        assert resp.status_code == 400
        assert mock_create.call_count == 1

    @polar_configured
    def test_business_plan_requires_sales(self):
        user = UserFactory(plan="starter")
        resp = self._client(user).post(
            "/api/v1/billing/checkout/", {"plan": "business"}, format="json",
        )
        assert resp.status_code == 400

    @polar_configured
    def test_active_subscriber_cannot_double_checkout(self):
        user = UserFactory(plan="pro")
        Subscription.objects.create(
            user=user,
            polar_subscription_id="ps_1", plan=Plan.PRO,
            status=SubscriptionStatus.ACTIVE,
        )
        resp = self._client(user).post(
            "/api/v1/billing/checkout/", {"plan": "pro"}, format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestBillingOverview:
    def test_overview_reports_billing_readiness_and_plan(self):
        user = UserFactory(plan="individual")
        client = APIClient()
        client.force_authenticate(user=user)
        data = client.get("/api/v1/billing/").json()["data"]
        # No Polar products configured in tests -> checkout must be
        # advertised as unavailable, and an unsubscribed account is Free.
        assert data["billing_ready"] is False
        assert data["plan"] == "free"
        assert data["plan_details"]["id"] == "free"

    @polar_configured
    def test_overview_ready_when_configured(self):
        user = UserFactory(plan="individual")
        client = APIClient()
        client.force_authenticate(user=user)
        # The stale-state safety net consults Polar for sub-less users;
        # model "no customer" so the test stays offline.
        with patch.object(
            polar_client, "get_customer_state",
            side_effect=polar_client.PolarRejected("no customer"),
        ):
            data = client.get("/api/v1/billing/").json()["data"]
        assert data["billing_ready"] is True
        assert data["plan"] == "free"
        assert data["subscription_status"] == "none"

    @polar_configured
    def test_trialing_subscription_shows_paid_plan(self):
        # The complaint this guards against: an account that paid must
        # NEVER render as Free on the billing page.
        user = UserFactory(plan="starter")
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.TRIALING,
            plan=Plan.PRO, polar_subscription_id="ps_123",
        )
        client = APIClient()
        client.force_authenticate(user=user)
        data = client.get("/api/v1/billing/").json()["data"]
        assert data["plan"] == "pro"
        assert data["subscription_status"] == "trialing"
        assert data["subscription_plan"] == "pro"
        # ...but it is described as a trial, never as a paid Pro plan.
        assert data["is_trialing"] is True
        assert data["tier"] == "pro"

    @polar_configured
    def test_overview_self_heals_stale_local_state(self):
        # Paid in Polar, but the confirm redirect never landed (no local
        # row). Opening the billing page must repair the record and show
        # the paid plan, not Free.
        user = UserFactory(plan="starter")
        client = APIClient()
        client.force_authenticate(user=user)
        with patch.object(
            polar_client, "get_customer_state",
            return_value=_state([_polar_sub(status="trialing")]),
        ):
            data = client.get("/api/v1/billing/").json()["data"]
        assert data["plan"] == "pro"
        assert data["subscription_status"] == "trialing"
        sub = Subscription.objects.get(user=user)
        assert sub.polar_subscription_id == "ps_123"

    @polar_configured
    def test_past_due_exposes_subscription_plan(self):
        # Feature gates close (top-level plan is free) but the UI keeps
        # showing the paid plan via subscription_plan + past_due status.
        user = UserFactory(plan="starter")
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.PAST_DUE,
            plan=Plan.PRO, polar_subscription_id="ps_9",
        )
        client = APIClient()
        client.force_authenticate(user=user)
        data = client.get("/api/v1/billing/").json()["data"]
        assert data["plan"] == "free"
        assert data["subscription_plan"] == "pro"
        assert data["subscription_status"] == "past_due"


@pytest.mark.django_db
class TestPolarWebhook:
    URL = "/api/v1/billing/polar/webhook/"

    def test_invalid_signature_rejected(self):
        with patch.object(
            polar_client, "validate_webhook",
            side_effect=polar_client.WebhookInvalid("bad signature"),
        ):
            resp = APIClient().post(self.URL, {"type": "x"}, format="json")
        assert resp.status_code == 400

    @polar_configured
    def test_state_changed_syncs_user(self):
        user = UserFactory(plan="starter")
        event = {
            "type": "customer.state_changed",
            "data": {"external_id": str(user.id)},
        }
        with patch.object(polar_client, "validate_webhook", return_value=event), patch.object(
            polar_client, "get_customer_state", return_value=_state([_polar_sub()])
        ):
            resp = APIClient().post(
                self.URL, {}, format="json", headers={"webhook-id": "wh_1"},
            )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.plan == Plan.PRO
        assert BillingEvent.objects.filter(event_id="polar_wh_1", processed=True).exists()

    @polar_configured
    def test_replay_is_idempotent(self):
        user = UserFactory(plan="starter")
        event = {
            "type": "customer.state_changed",
            "data": {"external_id": str(user.id)},
        }
        with patch.object(polar_client, "validate_webhook", return_value=event), patch.object(
            polar_client, "get_customer_state", return_value=_state([_polar_sub()])
        ) as mock_state:
            APIClient().post(self.URL, {}, format="json", headers={"webhook-id": "wh_2"})
            APIClient().post(self.URL, {}, format="json", headers={"webhook-id": "wh_2"})
        assert mock_state.call_count == 1


@pytest.mark.django_db
class TestSessionIsPaying:
    def test_trialing_polar_subscription_counts_as_paying(self):
        from apps.websites.tests.factories import WebsiteFactory

        user = UserFactory(plan="pro")
        WebsiteFactory(user=user)
        Subscription.objects.create(
            user=user,
            polar_subscription_id="ps_1", plan=Plan.PRO,
            status=SubscriptionStatus.TRIALING,
            current_period_start=datetime.now(UTC) - timedelta(days=1),
            current_period_end=datetime.now(UTC) + timedelta(days=6),
        )
        client = APIClient()
        client.force_authenticate(user=user)
        data = client.get("/api/v1/auth/session/").data
        assert data["subscription"]["is_paying"] is True
        assert data["subscription"]["is_trialing"] is True
        assert data["subscription"]["plan"] == "pro"
        assert data["subscription"]["trial_end"] is not None
        assert data["next_route"] == "app"
