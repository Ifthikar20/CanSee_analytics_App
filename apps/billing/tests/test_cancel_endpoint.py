"""POST /api/v1/billing/cancel/ and /resume/ — the self-serve cancellation.

This flow is load-bearing for consumer-protection compliance ("as easy to
cancel as to subscribe"): one button on the billing page, one confirm, one
POST, no support ticket. Until this file, nothing tested the endpoint at
all — the only cancellation coverage was a service-level sync test.

Cancellation is end-of-period, not immediate: the endpoint flips
cancel_at_period_end and access continues until current_period_end, which
is also what the frontend copy promises.
"""
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import Subscription
from core.utils.constants import Plan, SubscriptionStatus


def _user(email=None):
    from apps.accounts.models import User

    return User.objects.create_user(
        email=email or f"cancel-{uuid.uuid4().hex[:8]}@example.com",
        password="TestPass123!",
        full_name="Cancel Tester",
    )


def payload(resp):
    """Unwrap the response body.

    The view already returns {"success": ..., "data": ...} and the global
    EnvelopeRenderer wraps successful responses again, so the interesting
    dict sits one level deeper than usual. Asserted explicitly in
    test_response_shape so the quirk is visible, not hidden by this helper.
    """
    body = resp.json()
    inner = body.get("data")
    if isinstance(inner, dict) and "data" in inner and "success" in inner:
        return inner["data"]
    return inner


class CancelEndpointTest(TestCase):
    url = "/api/v1/billing/cancel/"
    resume_url = "/api/v1/billing/resume/"

    def setUp(self):
        self.user = _user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sub(self, *, polar_id=None):
        return Subscription.objects.create(
            user=self.user,
            polar_subscription_id=polar_id,
            plan=Plan.INDIVIDUAL,
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=21),
        )

    # ── Local-only subscription (no Polar id) ──

    def test_cancel_flips_the_flag_and_keeps_access_until_period_end(self):
        sub = self._sub()
        resp = self.client.post(self.url)

        assert resp.status_code == 200
        data = payload(resp)
        assert data["cancel_at_period_end"] is True
        assert data["current_period_end"] is not None

        sub.refresh_from_db()
        assert sub.cancel_at_period_end is True
        # Cancellation is end-of-period: status stays ACTIVE, nothing is
        # revoked today.
        assert sub.status == SubscriptionStatus.ACTIVE

    def test_resume_undoes_a_pending_cancellation(self):
        sub = self._sub()
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end"])

        resp = self.client.post(self.resume_url)

        assert resp.status_code == 200
        assert payload(resp)["cancel_at_period_end"] is False
        sub.refresh_from_db()
        assert sub.cancel_at_period_end is False

    # ── Polar-managed subscription ──

    def test_managed_subscription_cancels_through_the_provider(self):
        sub = self._sub(polar_id="polar_sub_123")

        def fake_set(user, *, cancel):
            sub.cancel_at_period_end = cancel
            sub.save(update_fields=["cancel_at_period_end"])
            return sub

        with patch(
            "apps.billing.services.polar_billing.set_cancel_at_period_end",
            side_effect=fake_set,
        ) as mocked:
            resp = self.client.post(self.url)

        assert resp.status_code == 200
        mocked.assert_called_once()
        assert mocked.call_args.kwargs["cancel"] is True
        assert payload(resp)["cancel_at_period_end"] is True

    def test_provider_failure_is_a_clean_400_not_a_silent_success(self):
        self._sub(polar_id="polar_sub_123")

        with patch(
            "apps.billing.services.polar_billing.set_cancel_at_period_end",
            side_effect=RuntimeError("polar down"),
        ):
            resp = self.client.post(self.url)

        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "billing_failed"
        # The local row must not have flipped when the provider call failed:
        # telling a user they cancelled while Polar keeps charging them is
        # the worst possible outcome of this endpoint.
        sub = Subscription.objects.get(user=self.user)
        assert sub.cancel_at_period_end is False

    # ── Edge cases ──

    def test_no_subscription_is_a_clean_400(self):
        resp = self.client.post(self.url)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_subscription"

    def test_unauthenticated_is_rejected(self):
        resp = APIClient().post(self.url)
        assert resp.status_code == 401

    def test_response_shape_documents_the_double_envelope(self):
        """The view hand-builds {"success", "data"} and EnvelopeRenderer
        wraps it again. The frontend reads through both layers today; if
        this test starts failing because the envelope became single, that
        is an API contract change and ftb-ui's billing.js must move in
        step."""
        self._sub()
        body = self.client.post(self.url).json()
        assert body["success"] is True
        assert body["data"]["success"] is True
        assert "cancel_at_period_end" in body["data"]["data"]
