"""
Tests for the PAYWALL_ENABLED master switch.

When PAYWALL_ENABLED is False (the default):
    - SessionView never returns next_route == "paywall"; onboarding
      still comes first, then users go straight to the app.
    - Every plan-entitlement layer resolves to the top tier: PLAN_LIMITS
      helpers, PLAN_FEATURES checks, DRF PlanFeatureRequired, the
      integration registry, and the audit prompt cap.

When PAYWALL_ENABLED is True the original gating behaviour applies
unchanged, so flipping the env var restores the paywall with no code
change.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.billing.services import plan_limits
from apps.websites.tests.factories import WebsiteFactory
from core.integrations import get_registry
from core.permissions.feature_flags import (
    get_team_member_limit,
    user_has_feature,
    user_has_integration,
)
from core.permissions.rbac import PlanFeatureRequired
from core.utils.constants import SubscriptionStatus, max_prompts_for_user

SESSION_URL = "/api/v1/auth/session/"


@pytest.fixture
def user(db):
    return UserFactory(plan="starter")


def _session(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client.get(SESSION_URL).data


@pytest.mark.django_db
class TestSessionRouting:
    def test_paywall_off_unpaid_user_goes_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = False
        WebsiteFactory(user=user)
        data = _session(user)
        assert data["next_route"] == "app"
        assert data["subscription"]["is_paying"] is False

    def test_paywall_on_unpaid_user_goes_to_paywall(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        assert _session(user)["next_route"] == "paywall"

    def test_onboarding_still_first_with_paywall_off(self, user, settings):
        settings.PAYWALL_ENABLED = False
        assert _session(user)["next_route"] == "onboarding"

    def test_paywall_on_paying_user_goes_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.ACTIVE, plan="starter",
        )
        data = _session(user)
        assert data["next_route"] == "app"
        assert data["subscription"]["is_paying"] is True

    def test_paywall_on_dismissed_user_goes_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        user.paywall_dismissed_at = timezone.now()
        user.save(update_fields=["paywall_dismissed_at", "updated_at"])
        data = _session(user)
        assert data["next_route"] == "app"
        assert data["paywall_dismissed"] is True

    def test_paywall_off_dismissed_user_goes_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = False
        WebsiteFactory(user=user)
        user.paywall_dismissed_at = timezone.now()
        user.save(update_fields=["paywall_dismissed_at", "updated_at"])
        assert _session(user)["next_route"] == "app"

    def test_onboarding_still_first_when_dismissed(self, user, settings):
        settings.PAYWALL_ENABLED = True
        user.paywall_dismissed_at = timezone.now()
        user.save(update_fields=["paywall_dismissed_at", "updated_at"])
        assert _session(user)["next_route"] == "onboarding"

    def test_paywall_on_dismissed_paying_user_goes_to_app(self, user, settings):
        settings.PAYWALL_ENABLED = True
        WebsiteFactory(user=user)
        user.paywall_dismissed_at = timezone.now()
        user.save(update_fields=["paywall_dismissed_at", "updated_at"])
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.ACTIVE, plan="pro",
        )
        data = _session(user)
        assert data["next_route"] == "app"
        assert data["subscription"]["is_paying"] is True


@pytest.mark.django_db
class TestEntitlementBypass:
    def test_plan_limits_resolve_to_top_tier(self, user, settings):
        settings.PAYWALL_ENABLED = False
        assert plan_limits.check_feature(user, "trend_intelligence") is True
        assert plan_limits.get_numeric_limit(user, "projects") == -1
        assert plan_limits.is_within_limit(user, "projects", 100) is True

    def test_plan_limits_gate_when_enabled(self, user, settings):
        settings.PAYWALL_ENABLED = True
        # No subscription resolves to the Free plan: 1 project, no SSO.
        # user.plan ("starter") is deliberately ignored — it defaults to
        # a paid tier and used to leak paid limits.
        assert plan_limits.check_feature(user, "sso") is False
        assert plan_limits.get_numeric_limit(user, "projects") == 1

    def test_plan_limits_gate_paid_tier(self, user, settings):
        settings.PAYWALL_ENABLED = True
        Subscription.objects.create(
            user=user, status=SubscriptionStatus.ACTIVE, plan="starter",
        )
        # An active legacy-starter subscription maps to the Pro tier
        # ($45): 5 projects, still no SSO.
        assert plan_limits.check_feature(user, "sso") is False
        assert plan_limits.get_numeric_limit(user, "projects") == 5

    def test_plan_features_bypass(self, user, settings):
        settings.PAYWALL_ENABLED = False
        assert user_has_feature(user, "white_label") is True
        assert get_team_member_limit(user) == 9999
        settings.PAYWALL_ENABLED = True
        assert user_has_feature(user, "white_label") is False
        # No subscription resolves to the Free plan: 1 team member.
        assert get_team_member_limit(user) == 1

    def test_integration_registry_bypass(self, user, settings):
        # hubspot is disabled on starter, enabled on growth/scale
        settings.PAYWALL_ENABLED = False
        assert user_has_integration(user, "hubspot") is True
        settings.PAYWALL_ENABLED = True
        assert user_has_integration(user, "hubspot") is False

    def test_registry_unknown_integration_still_denied(self, user, settings):
        settings.PAYWALL_ENABLED = False
        assert get_registry().check_entitlement(user, "does-not-exist") is False

    def test_registry_limit_uses_top_tier(self, user, settings):
        settings.PAYWALL_ENABLED = False
        off_limit = get_registry().get_limit(user, "webhooks", "max_endpoints", default=0)
        settings.PAYWALL_ENABLED = True
        on_limit = get_registry().get_limit(user, "webhooks", "max_endpoints", default=0)
        assert off_limit != 0
        assert on_limit == 0  # webhooks disabled for starter

    def test_drf_permission_bypass(self, user, settings, rf):
        class _Perm(PlanFeatureRequired):
            required_feature = "white_label"

        request = rf.get("/")
        request.user = user
        settings.PAYWALL_ENABLED = False
        assert _Perm().has_permission(request, view=None) is True
        settings.PAYWALL_ENABLED = True
        assert _Perm().has_permission(request, view=None) is False

    def test_max_prompts_uses_enterprise_cap(self, user, settings):
        settings.PAYWALL_ENABLED = False
        assert max_prompts_for_user(user) == 50
        settings.PAYWALL_ENABLED = True
        assert max_prompts_for_user(user) == 5  # no subscription -> Free cap
