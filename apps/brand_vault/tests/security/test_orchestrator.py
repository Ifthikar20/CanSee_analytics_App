"""Orchestrator tests — ensure agent rows are seeded and scheduling advances."""
from __future__ import annotations

import pytest

from apps.brand_vault.models import BrandSecurityAgent
from apps.brand_vault.services.security.orchestrator import (
    ensure_agent_rows,
    run_agent,
    run_all_agents,
)
from apps.brand_vault.services.security.registry import AGENTS
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


def test_ensure_agent_rows_seeds_missing_rows():
    website = WebsiteFactory()
    assert BrandSecurityAgent.objects.filter(website=website).count() == 0
    rows = ensure_agent_rows(website)
    assert len(rows) == len(AGENTS)
    assert BrandSecurityAgent.objects.filter(website=website).count() == len(AGENTS)


def test_run_agent_marks_next_run_at(monkeypatch):
    website = WebsiteFactory()
    row = BrandSecurityAgent.objects.create(
        website=website, agent_id="llm_truth",
        schedule=BrandSecurityAgent.SCHEDULE_DAILY,
    )

    # Neutralise the actual scan so we test only bookkeeping.
    from apps.brand_vault.services.security.registry import get_agent
    monkeypatch.setattr(
        get_agent("llm_truth"), "scan",
        lambda website, agent_row: [],
        raising=False,
    )

    result = run_agent(website, "llm_truth")
    assert result["status"] == BrandSecurityAgent.STATUS_IDLE
    row.refresh_from_db()
    assert row.last_run_at is not None
    assert row.next_run_at is not None
    assert row.next_run_at > row.last_run_at


def test_run_all_agents_returns_aggregate_stats(monkeypatch):
    website = WebsiteFactory()
    # Patch every registered agent's scan() to a no-op so we test aggregation.
    from apps.brand_vault.services.security.registry import AGENTS as _AGENTS
    for cls in _AGENTS.values():
        monkeypatch.setattr(cls, "scan", lambda self, website, row: [])
    result = run_all_agents(website)
    assert result["total_alerts"] == 0
    assert {row["agent_id"] for row in result["agents"]} == set(_AGENTS)


def test_run_agent_returns_error_status_when_scan_raises(monkeypatch):
    website = WebsiteFactory()
    BrandSecurityAgent.objects.create(website=website, agent_id="llm_truth")
    from apps.brand_vault.services.security.registry import AGENTS as _AGENTS

    def _boom(self, website, row):
        raise RuntimeError("nope")
    monkeypatch.setattr(_AGENTS["llm_truth"], "scan", _boom)

    result = run_agent(website, "llm_truth")
    assert result["status"] == BrandSecurityAgent.STATUS_ERROR
