"""Contract tests for the rewritten usage endpoints.

Settings (/auth/me/ai-usage/) and Billing (/billing/token-usage/) now share
one service, one window (the billing period), and one allowance shape — and
the fabricated capacity_tokens denominator is gone from both.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import AITokenUsage
from apps.accounts.tests.factories import UserFactory


def _usage_row(user, tokens=100, cost="0.10"):
    return AITokenUsage.objects.create(
        user=user, module="llm_ranking", provider="anthropic",
        model_name="claude-haiku-4-5", input_tokens=tokens // 2,
        output_tokens=tokens - tokens // 2, total_tokens=tokens,
        estimated_cost_usd=Decimal(cost), metadata={"role": "upstream"},
    )


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestAIUsageView:
    def test_requires_auth(self):
        assert APIClient().get("/api/v1/auth/me/ai-usage/").status_code == 401

    def test_period_shape_and_no_fabricated_capacity(self):
        user = UserFactory(plan="individual")
        _usage_row(user, tokens=400, cost="0.25")

        resp = _client(user).get("/api/v1/auth/me/ai-usage/")
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert data["source"] in ("local", "polar")
        assert data["period"]["source"] in ("calendar", "subscription")
        assert data["totals"]["total_tokens"] == 400
        # No subscription -> Free plan allowance, never a paid tier's.
        assert data["allowance"]["plan"] == "free"
        assert data["allowance"]["cap_usd"] == pytest.approx(1.0)
        assert data["allowance"]["spent_usd"] == pytest.approx(0.25)
        assert data["allowance"]["used_tokens"] == 400
        assert "capacity_tokens" not in data["allowance"]
        assert data["daily"], "daily series should include today"

    def test_legacy_days_param_is_ignored_not_500(self):
        # The old view did a bare int() on ?days= and crashed on garbage.
        user = UserFactory(plan="individual")
        resp = _client(user).get("/api/v1/auth/me/ai-usage/", {"days": "banana"})
        assert resp.status_code == 200

    def test_agrees_with_billing_endpoint(self):
        user = UserFactory(plan="individual")
        _usage_row(user, tokens=250, cost="0.15")

        client = _client(user)
        settings_data = client.get("/api/v1/auth/me/ai-usage/").json()["data"]
        billing_data = client.get("/api/v1/billing/token-usage/").json()["data"]

        assert (
            settings_data["totals"]["total_tokens"]
            == billing_data["totals"]["total_tokens"]
            == 250
        )
        assert settings_data["period"] == billing_data["period"]
        assert (
            settings_data["allowance"]["spent_usd"]
            == billing_data["allowance"]["spent_usd"]
        )
