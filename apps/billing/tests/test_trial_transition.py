"""The trial -> paid transition.

Polar charges the card at trial end and the webhook mirrors the new
status — except when no webhook can arrive (dev) or one was missed. These
tests pin the safety nets around that: a TRIALING row stops granting
anything once it is lapsed (trial end + grace), ended trials are
re-verified against Polar on request paths, a vanished subscription is
looked up by id so a failed charge lands as PAST_DUE, and every payload
describes a trial as a trial (not as "Pro"). All polar_client calls are
mocked — no network.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.billing.services import plan_limits, polar_billing
from apps.billing.tests.test_polar_billing import (
    MONTHLY_ID,
    _polar_sub,
    _state,
    polar_configured,
)
from apps.metering import polar_client
from core.utils.constants import Plan, SubscriptionStatus

H = timedelta(hours=1)
GRACE = plan_limits.TRIAL_LAPSE_GRACE


def _row(status=SubscriptionStatus.TRIALING, end=None, plan=Plan.PRO, polar_id="ps_123"):
    """Unsaved Subscription for the pure helpers."""
    return Subscription(
        plan=plan, status=status, polar_subscription_id=polar_id,
        current_period_start=end - timedelta(days=7) if end else None,
        current_period_end=end,
    )


def _trial_row(user, end, **overrides):
    fields = dict(
        user=user, polar_subscription_id="ps_123", plan=Plan.PRO,
        status=SubscriptionStatus.TRIALING,
        current_period_start=end - timedelta(days=7), current_period_end=end,
    )
    fields.update(overrides)
    return Subscription.objects.create(**fields)


def _polar_sub_by_id(user, status, product_id=MONTHLY_ID, external_id=None):
    """What ``subscriptions.get`` returns: full status enum + customer."""
    now = timezone.now()
    return SimpleNamespace(
        id="ps_123",
        status=status,
        product_id=product_id,
        customer=SimpleNamespace(external_id=external_id or str(user.id)),
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        cancel_at_period_end=False,
        started_at=now - timedelta(days=8),
    )


def _session(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client.get("/api/v1/auth/session/").data


def _overview(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client.get("/api/v1/billing/").json()["data"]


# ── Pure lapse guard ──────────────────────────────────────────────────────


class TestTrialLapse:
    def test_live_trial_grants_pro_and_counts_as_paying(self):
        sub = _row(end=timezone.now() + timedelta(days=5))
        assert plan_limits.plan_for_subscription(sub) == Plan.PRO
        assert plan_limits.is_paying_subscription(sub) is True
        assert plan_limits.is_trialing_subscription(sub) is True
        assert plan_limits.trial_ended(sub) is False

    def test_ended_trial_inside_grace_still_grants(self):
        # The webhook that converts the trial may be minutes (or, with
        # redelivery, hours) late — never flicker a converting customer.
        sub = _row(end=timezone.now() - H)
        assert plan_limits.trial_ended(sub) is True
        assert plan_limits.trial_lapsed(sub) is False
        assert plan_limits.plan_for_subscription(sub) == Plan.PRO
        assert plan_limits.is_paying_subscription(sub) is True
        assert plan_limits.is_trialing_subscription(sub) is True

    def test_lapsed_trial_grants_nothing_even_though_column_says_trialing(self):
        sub = _row(end=timezone.now() - GRACE - H)
        assert plan_limits.trial_lapsed(sub) is True
        assert plan_limits.plan_for_subscription(sub) == Plan.FREE
        assert plan_limits.is_paying_subscription(sub) is False
        assert plan_limits.is_trialing_subscription(sub) is False
        # The tier the row is FOR survives, for "Trial ended" labels.
        assert plan_limits.tier_for_subscription(sub) == Plan.PRO

    def test_explicit_now_is_honoured(self):
        end = timezone.now()
        sub = _row(end=end)
        assert plan_limits.trial_lapsed(sub, now=end + GRACE - H) is False
        assert plan_limits.trial_lapsed(sub, now=end + GRACE) is True

    def test_trial_without_dates_is_trusted(self):
        # Legacy / fixture rows carry no period: nothing to lapse against.
        sub = _row(end=None)
        assert plan_limits.trial_end_for(sub) is None
        assert plan_limits.plan_for_subscription(sub) == Plan.PRO
        assert plan_limits.is_paying_subscription(sub) is True

    def test_active_rows_never_lapse(self):
        # Renewals sit past current_period_end until subscription.cycled
        # lands; closing the gate there would flicker paying customers.
        sub = _row(status=SubscriptionStatus.ACTIVE, end=timezone.now() - timedelta(days=30))
        assert plan_limits.trial_end_for(sub) is None
        assert plan_limits.plan_for_subscription(sub) == Plan.PRO
        assert plan_limits.is_paying_subscription(sub) is True
        assert plan_limits.is_trialing_subscription(sub) is False

    def test_date_less_fake_subscription_does_not_explode(self):
        fake = SimpleNamespace(
            status=SubscriptionStatus.TRIALING, plan=Plan.PRO, polar_subscription_id="x",
        )
        assert plan_limits.plan_for_subscription(fake) == Plan.PRO
        assert plan_limits.is_paying_subscription(fake) is True
        assert plan_limits.subscription_state(fake)["trial_end"] is None

    def test_no_row(self):
        assert plan_limits.plan_for_subscription(None) == Plan.FREE
        assert plan_limits.is_paying_subscription(None) is False
        assert plan_limits.is_trialing_subscription(None) is False
        assert plan_limits.tier_for_subscription(None) is None


@pytest.mark.django_db
class TestLapsedTrialEntitlements:
    def test_lapsed_trial_falls_back_to_free_ai_cap(self, settings):
        from core.ai_tracking import effective_ai_cap

        user = UserFactory(plan="pro")
        _trial_row(user, timezone.now() - GRACE - H)
        assert effective_ai_cap(user) == settings.AI_FREE_MONTHLY_CAP_USD
        assert plan_limits.current_plan_for(user) == Plan.FREE
        assert plan_limits.is_paying(user) is False

    def test_live_trial_keeps_the_pro_ai_cap(self):
        from core.ai_tracking import effective_ai_cap

        user = UserFactory(plan="pro")
        _trial_row(user, timezone.now() + timedelta(days=5))
        assert effective_ai_cap(user) == round(45 * 0.65, 2)


# ── Vanished subscription: by-id lookup ───────────────────────────────────


@pytest.mark.django_db
class TestVanishedSubscriptionByIdLookup:
    """Customer state only lists active/trialing subscriptions. When ours
    drops out of it, the by-id read tells past_due from canceled."""

    def _managed_row(self, status=SubscriptionStatus.TRIALING):
        user = UserFactory(plan="pro")
        _trial_row(user, timezone.now() - H, status=status)
        return user

    def _sync(self, user, by_id):
        with (
            patch.object(polar_client, "get_customer_state", return_value=_state([])),
            patch.object(polar_client, "get_subscription", **by_id) as get_sub,
        ):
            sub = polar_billing.sync_from_customer_state(user)
        return sub, get_sub

    @polar_configured
    def test_failed_trial_charge_lands_as_past_due(self):
        user = self._managed_row()
        polar_sub = _polar_sub_by_id(user, "past_due")
        sub, get_sub = self._sync(user, {"return_value": polar_sub})
        get_sub.assert_called_once_with("ps_123")
        assert sub.status == SubscriptionStatus.PAST_DUE
        assert sub.plan == Plan.PRO
        assert sub.current_period_end == polar_sub.current_period_end
        # Gate closes, tier stays visible for the "payment issue" label.
        assert plan_limits.plan_for_subscription(sub) == Plan.FREE
        assert plan_limits.tier_for_subscription(sub) == Plan.PRO

    @polar_configured
    def test_dunning_exhausted_lands_as_past_due(self):
        user = self._managed_row()
        sub, _ = self._sync(user, {"return_value": _polar_sub_by_id(user, "unpaid")})
        assert sub.status == SubscriptionStatus.PAST_DUE

    @polar_configured
    def test_canceled_upstream_lands_as_canceled(self):
        user = self._managed_row()
        sub, _ = self._sync(user, {"return_value": _polar_sub_by_id(user, "canceled")})
        assert sub.status == SubscriptionStatus.CANCELED

    @polar_configured
    def test_enum_status_is_unwrapped_on_the_by_id_path(self):
        from polar_sdk.models import SubscriptionStatus as PolarStatus

        user = self._managed_row()
        sub, _ = self._sync(
            user, {"return_value": _polar_sub_by_id(user, PolarStatus.PAST_DUE)},
        )
        assert sub.status == SubscriptionStatus.PAST_DUE

    @polar_configured
    def test_state_lagging_behind_conversion_lands_as_active(self):
        # Customer state can briefly omit a subscription Polar just
        # cycled; the by-id read is authoritative.
        user = self._managed_row()
        sub, _ = self._sync(user, {"return_value": _polar_sub_by_id(user, "active")})
        assert sub.status == SubscriptionStatus.ACTIVE
        assert plan_limits.is_paying_subscription(sub) is True

    @polar_configured
    def test_gone_entirely_falls_back_to_canceled(self):
        user = self._managed_row()
        sub, _ = self._sync(user, {"side_effect": polar_client.PolarRejected("404")})
        assert sub.status == SubscriptionStatus.CANCELED

    @polar_configured
    def test_transient_failure_propagates_and_leaves_the_row_alone(self):
        user = self._managed_row(status=SubscriptionStatus.ACTIVE)
        with pytest.raises(polar_client.PolarUnavailable):
            self._sync(user, {"side_effect": polar_client.PolarUnavailable("503")})
        assert Subscription.objects.get(user=user).status == SubscriptionStatus.ACTIVE

    @polar_configured
    def test_subscription_owned_by_someone_else_is_not_mirrored(self):
        user = self._managed_row()
        sub, _ = self._sync(
            user,
            {"return_value": _polar_sub_by_id(user, "active", external_id="someone-else")},
        )
        assert sub.status == SubscriptionStatus.CANCELED

    @polar_configured
    def test_foreign_product_is_not_mirrored(self):
        user = self._managed_row()
        sub, _ = self._sync(
            user, {"return_value": _polar_sub_by_id(user, "active", product_id="other")},
        )
        assert sub.status == SubscriptionStatus.CANCELED

    @polar_configured
    def test_no_by_id_lookup_without_a_managed_row(self):
        user = UserFactory(plan="starter")
        sub, get_sub = self._sync(user, {"return_value": _polar_sub_by_id(user, "active")})
        get_sub.assert_not_called()
        assert sub.status == SubscriptionStatus.CANCELED  # empty sync never grants


# ── Ended-trial re-verify on request paths ────────────────────────────────


@pytest.mark.django_db
class TestEndedTrialReverify:
    def _ended_trial_user(self, hours_ago=2):
        from apps.websites.tests.factories import WebsiteFactory

        user = UserFactory(plan="pro")
        WebsiteFactory(user=user)  # past onboarding, so next_route is app/paywall
        _trial_row(user, timezone.now() - hours_ago * H)
        return user

    @polar_configured
    def test_session_settles_a_converted_trial_as_active(self):
        user = self._ended_trial_user()
        with patch.object(
            polar_client, "get_customer_state",
            return_value=_state([_polar_sub(status="active")]),
        ) as get_state:
            data = _session(user)
        get_state.assert_called_once()
        sub = data["subscription"]
        assert sub["status"] == "active"
        assert sub["is_trialing"] is False
        assert sub["trial_end"] is None
        assert sub["plan"] == "pro"
        assert sub["is_paying"] is True
        assert data["next_route"] == "app"
        assert Subscription.objects.get(user=user).status == SubscriptionStatus.ACTIVE

    @polar_configured
    def test_session_settles_a_failed_charge_as_past_due(self):
        user = self._ended_trial_user()
        with (
            patch.object(polar_client, "get_customer_state", return_value=_state([])),
            patch.object(
                polar_client, "get_subscription",
                return_value=_polar_sub_by_id(user, "past_due"),
            ),
        ):
            data = _session(user)
        sub = data["subscription"]
        assert sub["status"] == "past_due"
        assert sub["plan"] == "free"
        assert sub["tier"] == "pro"
        assert sub["is_paying"] is False
        assert sub["is_trialing"] is False

    @polar_configured
    def test_polar_outage_keeps_the_row_and_honours_the_grace_window(self):
        user = self._ended_trial_user(hours_ago=2)
        with patch.object(
            polar_client, "get_customer_state",
            side_effect=polar_client.PolarUnavailable("503"),
        ):
            data = _session(user)
        assert data["subscription"]["status"] == "trialing"
        assert data["subscription"]["is_paying"] is True  # inside grace
        assert data["subscription"]["is_trialing"] is True

    @polar_configured
    def test_polar_outage_past_grace_closes_the_gate(self):
        user = self._ended_trial_user(hours_ago=int(GRACE / H) + 1)
        with patch.object(
            polar_client, "get_customer_state",
            side_effect=polar_client.PolarUnavailable("503"),
        ):
            data = _session(user)
        sub = data["subscription"]
        assert sub["status"] == "trialing"  # column untouched
        assert sub["plan"] == "free"
        assert sub["is_paying"] is False
        assert sub["is_trialing"] is False
        assert sub["tier"] == "pro"

    @polar_configured
    def test_live_trial_does_not_touch_polar(self):
        from apps.websites.tests.factories import WebsiteFactory

        user = UserFactory(plan="pro")
        WebsiteFactory(user=user)
        _trial_row(user, timezone.now() + timedelta(days=5))
        with patch.object(polar_client, "get_customer_state") as get_state:
            data = _session(user)
        get_state.assert_not_called()
        assert data["subscription"]["is_trialing"] is True

    @polar_configured
    def test_reverify_is_cooldown_limited(self):
        user = self._ended_trial_user()
        with patch.object(
            polar_client, "get_customer_state",
            side_effect=polar_client.PolarUnavailable("503"),
        ) as get_state:
            _session(user)
            _session(user)
        assert get_state.call_count == 1

    def test_unconfigured_polar_is_never_called(self):
        user = self._ended_trial_user()
        with patch.object(polar_client, "get_customer_state") as get_state:
            data = _session(user)
        get_state.assert_not_called()
        assert data["subscription"]["status"] == "trialing"

    @polar_configured
    def test_overview_settles_an_ended_trial_too(self):
        user = self._ended_trial_user()
        with patch.object(
            polar_client, "get_customer_state",
            return_value=_state([_polar_sub(status="active")]),
        ):
            data = _overview(user)
        assert data["subscription_status"] == "active"
        assert data["is_trialing"] is False
        assert data["trial_end"] is None
        assert data["tier"] == "pro"
        assert data["plan"] == "pro"

    @polar_configured
    def test_checkout_is_not_trapped_by_a_stale_trial_row(self):
        # Trial ended, charge failed, Polar canceled it — a fresh checkout
        # must proceed instead of raising already_subscribed.
        user = self._ended_trial_user()
        checkout = SimpleNamespace(id="co_1", url="https://polar.sh/c/1")
        with (
            patch.object(polar_client, "get_customer_state", return_value=_state([])),
            patch.object(
                polar_client, "get_subscription",
                return_value=_polar_sub_by_id(user, "canceled"),
            ),
            patch.object(polar_client, "create_checkout", return_value=checkout) as create,
        ):
            url = polar_billing.create_checkout(user, annual=False, origin="http://t")
        create.assert_called_once()
        assert url.startswith("https://polar.sh/c/1")
        assert Subscription.objects.get(user=user).status == SubscriptionStatus.CANCELED


# ── Payload contract ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSessionPayloadContract:
    def _user(self):
        from apps.websites.tests.factories import WebsiteFactory

        user = UserFactory(plan="starter")
        WebsiteFactory(user=user)
        return user

    def test_live_trial_is_described_as_a_trial(self):
        user = self._user()
        end = timezone.now() + timedelta(days=5)
        _trial_row(user, end)
        sub = _session(user)["subscription"]
        assert sub == {
            "status": "trialing",
            "plan": "pro",
            "tier": "pro",
            "is_paying": True,
            "is_trialing": True,
            "interval": None,
            "trial_end": end.isoformat(),
            "current_period_end": end.isoformat(),
            "cancel_at_period_end": False,
        }

    def test_cancelled_trial_still_reports_its_end(self):
        user = self._user()
        end = timezone.now() + timedelta(days=2)
        _trial_row(user, end, cancel_at_period_end=True)
        sub = _session(user)["subscription"]
        assert sub["is_trialing"] is True
        assert sub["cancel_at_period_end"] is True
        assert sub["trial_end"] == end.isoformat()

    def test_legacy_plan_value_is_resolved(self):
        user = self._user()
        Subscription.objects.create(
            user=user, plan="starter", status=SubscriptionStatus.ACTIVE,
            polar_subscription_id="ps_1",
        )
        sub = _session(user)["subscription"]
        assert sub["plan"] == "pro"
        assert sub["tier"] == "pro"
        assert sub["is_trialing"] is False
        assert sub["trial_end"] is None

    def test_no_subscription(self):
        sub = _session(self._user())["subscription"]
        assert sub == {
            "status": None,
            "plan": "free",
            "tier": None,
            "is_paying": False,
            "is_trialing": False,
            "interval": None,
            "trial_end": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
        }

    def test_past_due_keeps_the_tier_but_not_access(self):
        user = self._user()
        Subscription.objects.create(
            user=user, plan=Plan.PRO, status=SubscriptionStatus.PAST_DUE,
            polar_subscription_id="ps_1",
        )
        sub = _session(user)["subscription"]
        assert sub["plan"] == "free"
        assert sub["tier"] == "pro"
        assert sub["is_paying"] is False
        assert sub["is_trialing"] is False


# ── polar_client wrapper ──────────────────────────────────────────────────


class TestPolarClientGetSubscription:
    @polar_configured
    def test_passes_the_id_through_the_guard(self):
        calls = {}

        class _Subs:
            def get(self, *, id):
                calls["id"] = id
                return SimpleNamespace(id=id, status="active")

        fake_client = SimpleNamespace(subscriptions=_Subs())
        with patch.object(polar_client, "get_client", return_value=fake_client):
            result = polar_client.get_subscription("ps_42")
        assert calls == {"id": "ps_42"}
        assert result.id == "ps_42"
