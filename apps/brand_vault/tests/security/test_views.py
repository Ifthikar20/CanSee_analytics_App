"""API contract tests for the /api/v1/brand-security/ endpoints."""
from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.brand_vault.models import (
    BrandSecurityAgent,
    BrandSecurityConfig,
    SafetyAlert,
    SafetyPrompt,
)
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _make_alert(website, **kwargs):
    defaults = dict(
        website=website,
        agent_id="llm_truth",
        source=SafetyAlert.SOURCE_LLM,
        title="Is Acme legit?",
        snippet="Acme is a scam.",
        issue=SafetyAlert.ISSUE_HALLUCINATION,
        severity=SafetyAlert.SEVERITY_HIGH,
        model="claude",
        prompt_text="Is Acme legit?",
    )
    defaults.update(kwargs)
    return SafetyAlert.objects.create(**defaults)


class TestOverview:
    def test_requires_auth(self):
        website = WebsiteFactory()
        url = reverse("brand-security-overview", args=[website.id])
        assert APIClient().get(url).status_code == 401

    def test_returns_health_and_counters(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        _make_alert(website, severity="high")
        _make_alert(website, severity="medium",
                    agent_id="serp_reputation", source=SafetyAlert.SOURCE_SERP)
        SafetyPrompt.objects.create(website=website, text="Is Acme legit?")

        resp = client.get(reverse("brand-security-overview", args=[website.id]))
        assert resp.status_code == 200
        body = resp.data
        assert body["open_alerts"] == 2
        assert body["by_severity"]["high"] == 1
        assert body["by_severity"]["medium"] == 1
        # Health = 100 - (10*1 + 4*1) = 86
        assert body["health_score"] == 86
        assert body["prompts_monitored"] == 1
        assert body["by_agent"]["llm_truth"] == 1
        assert body["by_agent"]["serp_reputation"] == 1


class TestAgentsList:
    def test_seeds_agent_rows_on_first_call(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        assert BrandSecurityAgent.objects.filter(website=website).count() == 0
        resp = client.get(reverse("brand-security-agents", args=[website.id]))
        assert resp.status_code == 200
        assert len(resp.data) == 5  # 5 v1 agents
        names = {row["agent_id"] for row in resp.data}
        assert "llm_truth" in names
        assert "narrative_watch" in names
        assert BrandSecurityAgent.objects.filter(website=website).count() == 5

    def test_reports_open_alert_counts_per_agent(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        _make_alert(website, agent_id="llm_truth", severity="high")
        _make_alert(website, agent_id="llm_truth", severity="low",
                    title="Different")

        resp = client.get(reverse("brand-security-agents", args=[website.id]))
        row = next(r for r in resp.data if r["agent_id"] == "llm_truth")
        assert row["open_alerts"] == 2
        assert row["open_high"] == 1
        assert row["open_low"] == 1


class TestAgentPatchAndRun:
    def test_patch_updates_sensitivity(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        BrandSecurityAgent.objects.create(
            website=website, agent_id="llm_truth", sensitivity="medium",
        )
        url = reverse(
            "brand-security-agent-detail",
            kwargs={"website_id": website.id, "agent_id": "llm_truth"},
        )
        resp = client.patch(url, {"sensitivity": "high", "enabled": False}, format="json")
        assert resp.status_code == 200
        row = BrandSecurityAgent.objects.get(website=website, agent_id="llm_truth")
        assert row.sensitivity == "high"
        assert row.enabled is False



class TestAlerts:
    def test_filter_by_agent(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        _make_alert(website, agent_id="llm_truth")
        _make_alert(website, agent_id="serp_reputation",
                    source=SafetyAlert.SOURCE_SERP, title="X")
        url = reverse("brand-security-alerts", args=[website.id])
        resp = client.get(f"{url}?agent_id=llm_truth")
        results = resp.data.get("results", resp.data)
        assert len(results) == 1
        assert results[0]["agent_id"] == "llm_truth"

    def test_resolve_marks_alert(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        alert = _make_alert(website)
        url = reverse(
            "brand-security-alert-action",
            kwargs={"alert_id": alert.id, "action": "resolve"},
        )
        resp = client.post(url)
        assert resp.status_code == 200
        alert.refresh_from_db()
        assert alert.status == SafetyAlert.STATUS_RESOLVED
        assert alert.resolved_by_id == user.id


class TestConfig:
    def test_get_creates_config_lazily(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        url = reverse("brand-security-config", args=[website.id])
        resp = client.get(url)
        assert resp.status_code == 200
        assert BrandSecurityConfig.objects.filter(website=website).exists()

    def test_put_writes_terms(self, auth):
        client, user = auth
        website = WebsiteFactory(user=user)
        url = reverse("brand-security-config", args=[website.id])
        resp = client.put(
            url,
            {"brand_terms": ["Acme", "acme.io"], "negative_keywords": ["scam"]},
            format="json",
        )
        assert resp.status_code == 200
        cfg = BrandSecurityConfig.objects.get(website=website)
        assert cfg.brand_terms == ["Acme", "acme.io"]
        assert cfg.negative_keywords == ["scam"]
