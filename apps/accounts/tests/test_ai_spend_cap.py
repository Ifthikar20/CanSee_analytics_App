"""Tests for the plan-derived monthly AI spend allowance.

The cap is margin protection, resolved from ACTUAL subscription status:
an active/trialing subscription grants 65% of its plan price as model
cost per billing period; everything else is the Free plan and gets the
small AI_FREE_MONTHLY_CAP_USD budget. Enforced at the provider choke
point so no call site can leak spend.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from core.ai_tracking import (
    AI_SPEND_CAP_RATIO,
    _estimate_cost,
    effective_ai_cap,
    month_to_date_cost,
)
from core.utils.constants import SubscriptionStatus


def _subscribe(user, plan="pro", status=SubscriptionStatus.ACTIVE):
    now = timezone.now()
    return Subscription.objects.create(
        user=user,
        plan=plan,
        status=status,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
    )


@pytest.mark.django_db
class TestEffectiveCap:
    def test_unsubscribed_user_gets_free_cap(self, settings):
        # user.plan defaults to a paid tier, but with no subscription the
        # account is on the Free plan — paid allowances must never leak.
        settings.AI_FREE_MONTHLY_CAP_USD = 1.0
        user = UserFactory(plan="individual")
        assert effective_ai_cap(user) == 1.0

    def test_active_pro_subscription_derives_65_pct(self):
        user = UserFactory(plan="pro")
        _subscribe(user, plan="pro")
        assert effective_ai_cap(user) == pytest.approx(45 * AI_SPEND_CAP_RATIO)

    def test_trialing_subscription_gets_plan_cap(self):
        user = UserFactory(plan="pro")
        _subscribe(user, plan="pro", status=SubscriptionStatus.TRIALING)
        assert effective_ai_cap(user) == pytest.approx(45 * AI_SPEND_CAP_RATIO)

    def test_canceled_subscription_falls_back_to_free(self, settings):
        settings.AI_FREE_MONTHLY_CAP_USD = 1.0
        user = UserFactory(plan="pro")
        _subscribe(user, plan="pro", status=SubscriptionStatus.CANCELED)
        assert effective_ai_cap(user) == 1.0

    def test_legacy_individual_subscription_resolves_to_pro_cap(self):
        user = UserFactory(plan="individual")
        _subscribe(user, plan="individual")
        assert effective_ai_cap(user) == pytest.approx(45 * AI_SPEND_CAP_RATIO)

    def test_manual_cap_only_tightens_paid_plans(self):
        user = UserFactory(plan="pro", monthly_ai_cost_cap_usd=Decimal("10"))
        _subscribe(user, plan="pro")
        assert effective_ai_cap(user) == 10.0
        # A manual cap looser than the plan allowance is clamped to the plan.
        user.monthly_ai_cost_cap_usd = Decimal("500")
        assert effective_ai_cap(user) == pytest.approx(45 * AI_SPEND_CAP_RATIO)

    def test_manual_cap_raises_free_accounts(self, settings):
        # Comped testers: an admin-granted budget overrides the free cap.
        settings.AI_FREE_MONTHLY_CAP_USD = 1.0
        user = UserFactory(plan="individual", monthly_ai_cost_cap_usd=Decimal("10"))
        assert effective_ai_cap(user) == 10.0

    def test_business_subscription_uses_finite_safety_ceiling(self, settings):
        settings.AI_ENTERPRISE_MONTHLY_CAP_USD = 500.0
        user = UserFactory(plan="business")
        _subscribe(user, plan="business")
        assert effective_ai_cap(user) == 500.0
        # A negotiated per-account cap overrides the deployment ceiling.
        user.monthly_ai_cost_cap_usd = Decimal("2000")
        assert effective_ai_cap(user) == 2000.0

    def test_business_unlimited_only_when_explicitly_opted_in(self, settings):
        settings.AI_ENTERPRISE_MONTHLY_CAP_USD = 0.0
        user = UserFactory(plan="business")
        _subscribe(user, plan="business")
        assert effective_ai_cap(user) == 0.0  # 0 = unlimited, opt-in only

    def test_none_user_unlimited(self):
        assert effective_ai_cap(None) == 0.0


class TestPricing:
    def test_unknown_model_falls_back_conservatively(self):
        # 1M in + 1M out on an unknown model = default $3 + $15.
        assert _estimate_cost("mystery-model", 1_000_000, 1_000_000) == 18.0

    def test_sonar_includes_per_call_fee(self):
        cost = _estimate_cost("sonar", 1000, 1000)
        token_part = (1000 / 1_000_000) * 1.0 * 2
        assert cost == pytest.approx(token_part + 0.008, abs=1e-6)

    def test_grok_and_gemini_priced(self):
        assert _estimate_cost("grok-4", 1_000_000, 0) == 3.0
        assert _estimate_cost("gemini-2.0-flash", 1_000_000, 0) == pytest.approx(0.10)


@pytest.mark.django_db
class TestChokePoint:
    def _spend(self, user, usd):
        # One synthetic row carrying an explicit cost.
        AITokenUsage.objects.create(
            user=user, module="test", provider="anthropic",
            model_name="claude-haiku-4-5", input_tokens=1, output_tokens=1,
            total_tokens=2, estimated_cost_usd=Decimal(str(usd)),
        )

    def test_query_refused_at_cap(self, monkeypatch):
        from apps.llm_ranking.providers.claude import ClaudeProvider

        user = UserFactory(plan="individual")
        _subscribe(user, plan="pro")
        self._spend(user, 45 * AI_SPEND_CAP_RATIO)  # exactly at the wall

        provider = ClaudeProvider()
        monkeypatch.setattr(provider, "api_key", "test-key")
        result = provider.query("hello", user=user)
        assert result.succeeded is False
        assert "monthly_ai_allowance_exceeded" in result.error

    def test_query_allowed_under_cap(self, monkeypatch):
        from apps.llm_ranking.providers.claude import ClaudeProvider

        user = UserFactory(plan="individual")
        _subscribe(user, plan="pro")
        self._spend(user, 1.0)

        provider = ClaudeProvider()
        monkeypatch.setattr(provider, "api_key", "test-key")
        # Under the cap the wall passes and the call proceeds to the SDK,
        # which we stub to prove the gate (not the network) decided.
        called = {}

        def fake_call(**kwargs):
            called["yes"] = True
            from apps.llm_ranking.providers.base import ProviderResult
            return ProviderResult(succeeded=True, text="ok")

        monkeypatch.setattr(provider, "_call", fake_call)
        result = provider.query("hello", user=user)
        assert called.get("yes") is True
        assert result.succeeded is True

    def test_unattributed_calls_exempt(self, monkeypatch):
        from apps.llm_ranking.providers.claude import ClaudeProvider

        provider = ClaudeProvider()
        monkeypatch.setattr(provider, "api_key", "test-key")

        def fake_call(**kwargs):
            from apps.llm_ranking.providers.base import ProviderResult
            return ProviderResult(succeeded=True, text="ok")

        monkeypatch.setattr(provider, "_call", fake_call)
        assert provider.query("health check").succeeded is True

    def test_attributed_call_denied_when_allowance_lookup_errors(self, monkeypatch):
        # Fail-closed: a broken cap lookup must DENY an attributed call, not
        # silently allow unbounded spend.
        from apps.llm_ranking.providers.claude import ClaudeProvider

        user = UserFactory(plan="individual")

        def boom(*_a, **_kw):
            raise RuntimeError("cap lookup exploded")

        monkeypatch.setattr("core.ai_tracking.effective_ai_cap", boom)
        provider = ClaudeProvider()
        monkeypatch.setattr(provider, "api_key", "test-key")
        # _call must never be reached; prove the gate denied it.
        monkeypatch.setattr(
            provider, "_call",
            lambda **kw: (_ for _ in ()).throw(AssertionError("should not call SDK")),
        )
        result = provider.query("hello", user=user)
        assert result.succeeded is False
        assert "ai_allowance_check_failed" in result.error


@pytest.mark.django_db
class TestMonthlyReset:
    def test_previous_month_spend_does_not_count(self):
        user = UserFactory(plan="individual")
        now = timezone.now()
        prev_month = (now.replace(day=1) - timezone.timedelta(days=1)).replace(day=15)
        row = AITokenUsage.objects.create(
            user=user, module="test", provider="anthropic",
            model_name="claude-haiku-4-5", input_tokens=1, output_tokens=1,
            total_tokens=2, estimated_cost_usd=Decimal("100"),
        )
        # created_at is auto_now_add; move it into last month directly.
        AITokenUsage.objects.filter(id=row.id).update(created_at=prev_month)
        assert month_to_date_cost(user) == 0.0
