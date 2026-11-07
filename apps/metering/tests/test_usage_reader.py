"""get_period_usage(): local aggregation, Polar reads, honest allowance."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory
from apps.metering import polar_client
from apps.metering.services.usage_reader import get_period_usage


def _usage_row(user, *, tokens=100, cost="0.10", module="llm_ranking", role="upstream"):
    return AITokenUsage.objects.create(
        user=user,
        module=module,
        provider="anthropic",
        model_name="claude-haiku-4-5",
        input_tokens=tokens // 2,
        output_tokens=tokens - tokens // 2,
        total_tokens=tokens,
        estimated_cost_usd=Decimal(cost),
        metadata={"role": role},
    )


@pytest.mark.django_db
class TestLocalReads:
    def test_shape_and_totals(self, settings):
        settings.AI_FREE_MONTHLY_CAP_USD = 1.0
        user = UserFactory(plan="individual")
        _usage_row(user, tokens=100, cost="0.10")
        _usage_row(user, tokens=300, cost="0.20", module="rag", role="embedding")

        usage = get_period_usage(user)

        assert usage["source"] == "local"
        assert usage["period"]["source"] == "calendar"
        assert usage["totals"]["calls"] == 2
        assert usage["totals"]["total_tokens"] == 400
        assert usage["totals"]["estimated_cost_usd"] == pytest.approx(0.30)
        assert {m["module"] for m in usage["by_module"]} == {"llm_ranking", "rag"}
        assert {r["role"] for r in usage["by_role"]} == {"upstream", "embedding"}
        assert len(usage["daily"]) == 1
        assert usage["daily"][0]["tokens"] == 400

        allowance = usage["allowance"]
        # No subscription -> the account is on the FREE plan and gets the
        # small free budget, never a paid tier's allowance.
        assert allowance["plan"] == "free"
        assert allowance["cap_usd"] == pytest.approx(1.0)
        assert allowance["spent_usd"] == pytest.approx(0.30)
        assert allowance["used_tokens"] == 400
        assert allowance["cap_source"] == "plan"
        # The fabricated denominator is gone for good.
        assert "capacity_tokens" not in allowance

    def test_subscribed_user_gets_plan_allowance(self):
        from datetime import timedelta

        from apps.billing.models import Subscription
        from core.utils.constants import SubscriptionStatus

        user = UserFactory(plan="pro")
        now = timezone.now()
        Subscription.objects.create(
            user=user, plan="pro",
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=29),
        )
        _usage_row(user, tokens=100, cost="0.10")
        usage = get_period_usage(user)
        assert usage["allowance"]["plan"] == "pro"
        assert usage["allowance"]["cap_usd"] == pytest.approx(45 * 0.65)
        assert usage["period"]["source"] == "subscription"

    def test_other_users_usage_excluded(self):
        user = UserFactory(plan="individual")
        other = UserFactory(plan="individual")
        _usage_row(other, tokens=5000)
        usage = get_period_usage(user)
        assert usage["totals"]["total_tokens"] == 0


def _provision(user):
    from apps.metering.models import PolarCustomer

    PolarCustomer.objects.create(
        user=user, polar_customer_id="pc_1", environment="sandbox",
    )


@pytest.mark.django_db
class TestPolarReads:
    @override_settings(
        POLAR_READS_ENABLED=True,
        POLAR_ACCESS_TOKEN="token",
        POLAR_METER_TOKENS_ID="meter-1",
        POLAR_ENVIRONMENT="sandbox",
    )
    def test_polar_totals_override_local(self):
        user = UserFactory(plan="individual")
        _provision(user)
        _usage_row(user, tokens=100, cost="0.10")

        today = timezone.now()
        fake = SimpleNamespace(
            quantities=[SimpleNamespace(timestamp=today, quantity=150)],
            total=150,
        )
        with patch.object(polar_client, "meter_quantities", return_value=fake):
            usage = get_period_usage(user)

        assert usage["source"] == "polar"
        assert usage["totals"]["total_tokens"] == 150
        day_row = next(
            d for d in usage["daily"] if d["day"] == today.date().isoformat()
        )
        assert day_row["tokens"] == 150
        # Tokens display follows Polar; dollars stay ledger-authoritative.
        assert usage["allowance"]["used_tokens"] == 150
        assert usage["allowance"]["spent_usd"] == pytest.approx(0.10)

    @override_settings(
        POLAR_READS_ENABLED=True,
        POLAR_ACCESS_TOKEN="token",
        POLAR_METER_TOKENS_ID="meter-1",
        POLAR_ENVIRONMENT="sandbox",
    )
    def test_polar_unavailable_falls_back_to_local(self):
        user = UserFactory(plan="individual")
        _provision(user)
        _usage_row(user, tokens=100)

        with patch.object(
            polar_client,
            "meter_quantities",
            side_effect=polar_client.PolarUnavailable("down"),
        ):
            usage = get_period_usage(user)

        assert usage["source"] == "local"
        assert usage["totals"]["total_tokens"] == 100

    @override_settings(POLAR_READS_ENABLED=False, POLAR_ACCESS_TOKEN="token",
                       POLAR_METER_TOKENS_ID="meter-1")
    def test_reads_flag_off_never_calls_polar(self):
        user = UserFactory(plan="individual")
        with patch.object(polar_client, "meter_quantities") as mock_q:
            usage = get_period_usage(user)
        assert mock_q.call_count == 0
        assert usage["source"] == "local"

    @override_settings(
        POLAR_READS_ENABLED=True,
        POLAR_ACCESS_TOKEN="token",
        POLAR_METER_TOKENS_ID="meter-1",
        POLAR_ENVIRONMENT="sandbox",
    )
    def test_unprovisioned_user_stays_on_local_reads(self):
        # A user Polar rejected (no PolarCustomer marker) must read the
        # local ledger even with reads enabled — Polar would report 0.
        user = UserFactory(plan="individual")
        _usage_row(user, tokens=100)
        with patch.object(polar_client, "meter_quantities") as mock_q:
            usage = get_period_usage(user)
        assert mock_q.call_count == 0
        assert usage["source"] == "local"
        assert usage["totals"]["total_tokens"] == 100
