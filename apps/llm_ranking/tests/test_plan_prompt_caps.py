"""
Asserts the per-plan ``max_prompts_per_audit`` cap is enforced both
in ``max_prompts_for_user`` (the lookup) and in
``LLMRankingService.generate_prompts`` (the consumer).
"""
from unittest.mock import MagicMock

import pytest

from core.utils.constants import Plan, SubscriptionStatus, max_prompts_for_user


@pytest.fixture(autouse=True)
def _paywall_on(settings):
    # These tests assert plan gating, which only applies while the
    # paywall is enabled (PAYWALL_ENABLED defaults to False).
    settings.PAYWALL_ENABLED = True


class _FakeSub:
    def __init__(self, plan, status=SubscriptionStatus.ACTIVE):
        self.plan = plan
        self.status = status


class _FakeUser:
    def __init__(self, plan):
        self.subscription = _FakeSub(plan) if plan else None


class TestMaxPromptsForUser:
    def test_pro_returns_15(self):
        assert max_prompts_for_user(_FakeUser(Plan.PRO)) == 15

    def test_business_returns_50(self):
        assert max_prompts_for_user(_FakeUser(Plan.BUSINESS)) == 50

    def test_legacy_individual_resolves_to_pro_limit(self):
        # The retired Individual tier aliases to Pro ($45).
        assert max_prompts_for_user(_FakeUser(Plan.INDIVIDUAL)) == 15

    def test_legacy_enterprise_resolves_to_business_limit(self):
        assert max_prompts_for_user(_FakeUser(Plan.ENTERPRISE)) == 50

    def test_no_subscription_falls_back_to_free(self):
        user = MagicMock()
        # Simulate the OneToOne reverse relation raising
        # Subscription.DoesNotExist when accessed.
        type(user).subscription = property(
            fget=lambda self: (_ for _ in ()).throw(AttributeError("missing")),
        )
        # The helper should not propagate the error; no subscription
        # resolves to the Free plan cap.
        assert max_prompts_for_user(user) == 5

    def test_unknown_plan_falls_back_to_pro(self):
        # An ACTIVE subscription on an unrecognised plan honours the
        # payment and resolves to the Pro cap.
        assert max_prompts_for_user(_FakeUser("ghost-tier")) == 15

    def test_legacy_starter_resolves_to_pro_limit(self):
        # Subscriptions on the old "starter" plan should keep working.
        assert max_prompts_for_user(_FakeUser(Plan.STARTER)) == 15


@pytest.mark.django_db
class TestGeneratePromptsRespectsPlan:
    """generate_prompts must cap its returned list at the user's plan limit."""

    def test_pro_returns_at_most_15(self, settings):
        # Disable the Claude variants path so the test stays hermetic
        # — the deterministic library alone produces > 15 prompts so we
        # can verify the cap is the binding constraint.
        settings.ANTHROPIC_API_KEY = ""

        from apps.llm_ranking.services.ranking_service import LLMRankingService
        prompts = LLMRankingService.generate_prompts(
            business_name="Acme",
            industry="SaaS analytics",
            description="Acme builds growth analytics for SaaS teams.",
            keywords=["analytics", "growth"],
            user=_FakeUser(Plan.PRO),
        )
        assert len(prompts) <= 15
        # Each prompt has the standard shape regardless of cap
        assert all("text" in p and "type" in p for p in prompts)

    def test_business_allows_more_than_pro(self, settings):
        settings.ANTHROPIC_API_KEY = ""
        from apps.llm_ranking.services.ranking_service import LLMRankingService
        pro = LLMRankingService.generate_prompts(
            business_name="Acme", industry="SaaS analytics",
            description="Growth analytics.",
            keywords=["analytics"],
            user=_FakeUser(Plan.PRO),
        )
        business = LLMRankingService.generate_prompts(
            business_name="Acme", industry="SaaS analytics",
            description="Growth analytics.",
            keywords=["analytics"],
            user=_FakeUser(Plan.BUSINESS),
        )
        assert len(business) >= len(pro)
        assert len(pro) <= 15

    def test_no_user_defaults_to_pro_cap(self, settings):
        settings.ANTHROPIC_API_KEY = ""
        from apps.llm_ranking.services.ranking_service import LLMRankingService
        prompts = LLMRankingService.generate_prompts(
            business_name="Acme", industry="SaaS analytics",
            description="x", keywords=["analytics"],
        )
        assert len(prompts) <= 15
