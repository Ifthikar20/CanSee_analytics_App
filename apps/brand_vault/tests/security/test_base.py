"""Tests for the Brand Security agent base class."""
from __future__ import annotations

import pytest

from apps.brand_vault.models import BrandSecurityAgent, SafetyAlert
from apps.brand_vault.services.security.base import (
    BaseSecurityAgent,
    Finding,
    Verdict,
)
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


class _StubAgent(BaseSecurityAgent):
    agent_id = "stub"
    display_name = "Stub"

    def __init__(self, findings, verdicts):
        self._findings = findings
        self._verdicts = verdicts
        self._i = 0

    def run(self, website, config):
        return list(self._findings)

    def judge(self, website, finding):
        v = self._verdicts[self._i]
        self._i += 1
        return v


def _finding(url="https://example.com/a", title="t", src=SafetyAlert.SOURCE_SERP):
    return Finding(source=src, title=title, snippet="s", source_url=url)


def test_scan_persists_alert_with_agent_attribution():
    website = WebsiteFactory()
    agent_row = BrandSecurityAgent.objects.create(
        website=website, agent_id="stub", sensitivity="medium",
    )
    agent = _StubAgent(
        findings=[_finding()],
        verdicts=[Verdict(issue="negative", severity="high", detail="bad")],
    )
    alerts = agent.scan(website, agent_row)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.agent_id == "stub"
    assert a.source == SafetyAlert.SOURCE_SERP
    assert a.severity == "high"
    assert a.issue == "negative"


def test_scan_drops_none_verdicts():
    website = WebsiteFactory()
    row = BrandSecurityAgent.objects.create(
        website=website, agent_id="stub", sensitivity="medium",
    )
    agent = _StubAgent(
        findings=[_finding()],
        verdicts=[Verdict()],  # issue=none
    )
    assert agent.scan(website, row) == []


def test_scan_respects_low_sensitivity():
    """Low sensitivity means only high-severity alerts should surface."""
    website = WebsiteFactory()
    row = BrandSecurityAgent.objects.create(
        website=website, agent_id="stub", sensitivity="low",
    )
    agent = _StubAgent(
        findings=[_finding(url="https://a"), _finding(url="https://b")],
        verdicts=[
            Verdict(issue="negative", severity="medium"),
            Verdict(issue="negative", severity="high"),
        ],
    )
    alerts = agent.scan(website, row)
    assert len(alerts) == 1
    assert alerts[0].severity == "high"


def test_scan_dedupes_open_alerts_by_source_url():
    website = WebsiteFactory()
    row = BrandSecurityAgent.objects.create(
        website=website, agent_id="stub", sensitivity="medium",
    )
    dup = "https://example.com/dup"
    agent = _StubAgent(
        findings=[_finding(url=dup), _finding(url=dup)],
        verdicts=[
            Verdict(issue="negative", severity="high"),
            Verdict(issue="negative", severity="high"),
        ],
    )
    alerts = agent.scan(website, row)
    assert len(alerts) == 1
    # The second run doesn't create a new alert while the first is open.
    agent2 = _StubAgent(
        findings=[_finding(url=dup)],
        verdicts=[Verdict(issue="negative", severity="high")],
    )
    assert agent2.scan(website, row) == []
