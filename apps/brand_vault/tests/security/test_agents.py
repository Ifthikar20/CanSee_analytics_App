"""Per-agent unit tests. All external calls (LLMs, Reddit, SERP, Trends) mocked."""
from __future__ import annotations

import pytest

from apps.brand_vault.models import (
    BrandSecurityAgent,
    BrandSecurityConfig,
    SafetyAlert,
    SafetyPrompt,
)
from apps.brand_vault.services.security.agents.impersonation import (
    ImpersonationAgent,
)
from apps.brand_vault.services.security.agents.llm_truth import LLMTruthAgent
from apps.brand_vault.services.security.agents.narrative_watch import (
    NarrativeWatchAgent,
)
from apps.brand_vault.services.security.agents.sentiment_pulse import (
    SentimentPulseAgent,
)
from apps.brand_vault.services.security.agents.serp_reputation import (
    SerpReputationAgent,
)
from apps.brand_vault.services.security.base import Verdict
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


def _website(brand="Acme", url="https://acme.io"):
    w = WebsiteFactory(name=brand, url=url)
    BrandSecurityConfig.objects.create(
        website=w, brand_terms=[brand], negative_keywords=["scam"],
    )
    return w


def _row(website, agent_id):
    return BrandSecurityAgent.objects.create(
        website=website, agent_id=agent_id, sensitivity="medium",
    )


# ── LLM Truth ─────────────────────────────────────────────────────────────

def test_llm_truth_creates_alert_from_provider_response(monkeypatch):
    website = _website()
    SafetyPrompt.objects.create(website=website, text="Tell me about Acme")

    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.llms.ask_all_providers",
        lambda prompt, **kw: [{
            "provider": "claude",
            "model": "claude-haiku-4-5",
            "text": "Acme is a defunct pyramid scheme.",
        }],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.llm_truth.judge_finding",
        lambda **kw: Verdict(
            issue=SafetyAlert.ISSUE_HALLUCINATION,
            severity="high",
            detail="False claim.",
        ),
    )

    alerts = LLMTruthAgent().scan(website, _row(website, "llm_truth"))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.agent_id == "llm_truth"
    assert a.source == SafetyAlert.SOURCE_LLM
    assert a.model == "claude-haiku-4-5"
    assert a.prompt_text == "Tell me about Acme"


def test_llm_truth_no_prompts_uses_defaults(monkeypatch):
    website = _website(brand="Acme")
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.llms.ask_all_providers",
        lambda prompt, **kw: [],
    )
    # No prompts configured. Agent should call the LLM anyway with defaults.
    calls = []
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.llms.ask_all_providers",
        lambda prompt, **kw: calls.append(prompt) or [],
    )
    LLMTruthAgent().scan(website, _row(website, "llm_truth"))
    assert any("Acme" in p for p in calls)


# ── SERP Reputation ───────────────────────────────────────────────────────

def test_serp_reputation_skips_own_domain_for_brand_query(monkeypatch):
    website = _website(brand="Acme", url="https://acme.io")

    def fake_search(query, num=10):
        if query == "Acme":
            return [
                {"url": "https://acme.io/home", "title": "Acme", "snippet": "",
                 "domain": "acme.io", "serp_rank": 1},
                {"url": "https://scam.example/acme",
                 "title": "Acme scam exposed", "snippet": "avoid",
                 "domain": "scam.example", "serp_rank": 2},
            ]
        return []

    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.serp.search",
        fake_search,
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.serp_reputation.judge_finding",
        lambda **kw: Verdict(
            issue=SafetyAlert.ISSUE_NEGATIVE_OUTRANKING,
            severity="high",
            detail="",
        ),
    )
    alerts = SerpReputationAgent().scan(website, _row(website, "serp_reputation"))
    assert len(alerts) == 1
    assert alerts[0].source_url == "https://scam.example/acme"


# ── Narrative Watch ───────────────────────────────────────────────────────

def test_narrative_watch_flags_rising_trends_and_reddit(monkeypatch):
    website = _website()
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.trends.rising_related",
        lambda term: [
            {"query": f"{term} scam", "value": 1500, "link": "https://t/1"},
            {"query": f"{term} coupon", "value": 200, "link": "https://t/2"},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        lambda term, limit=15: [
            {"title": "Acme is going down", "snippet": "boycott",
             "url": "https://reddit/a", "subreddit": "r/acme", "score": 12,
             "num_comments": 3, "created_utc": 0.0},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.narrative_watch.judge_finding",
        lambda **kw: Verdict(
            issue=SafetyAlert.ISSUE_EMERGING_NARRATIVE,
            severity="medium",
            detail="",
        ),
    )
    alerts = NarrativeWatchAgent().scan(website, _row(website, "narrative_watch"))
    # 1 rising (value>=500) + 1 reddit = 2
    assert len(alerts) == 2
    sources = {a.source for a in alerts}
    assert SafetyAlert.SOURCE_TRENDS in sources
    assert SafetyAlert.SOURCE_REDDIT in sources


# ── Sentiment Pulse ───────────────────────────────────────────────────────

def test_sentiment_pulse_reads_reddit_and_x(monkeypatch):
    website = _website()
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        lambda term, limit=25: [
            {"title": "I hate Acme", "snippet": "worst service",
             "url": "https://reddit/hate", "subreddit": "r/rants",
             "score": 5, "num_comments": 0, "created_utc": 0.0},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.x.search_mentions",
        lambda term, limit=25: [
            {"title": "Acme is trash", "snippet": "avoid",
             "url": "https://x.com/i/status/1", "author_id": "1",
             "created_at": ""},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.judge_finding",
        lambda **kw: Verdict(
            issue=SafetyAlert.ISSUE_NEGATIVE,
            severity="medium",
            sentiment_score=-0.8,
            detail="",
        ),
    )
    alerts = SentimentPulseAgent().scan(website, _row(website, "sentiment_pulse"))
    assert len(alerts) == 2
    assert {a.source for a in alerts} == {SafetyAlert.SOURCE_REDDIT, SafetyAlert.SOURCE_X}
    assert all(a.sentiment_score == -0.8 for a in alerts)


# ── Impersonation ─────────────────────────────────────────────────────────

def test_impersonation_flags_typosquat_domain(monkeypatch):
    website = _website(brand="Acme", url="https://acme.io")
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.serp.search",
        lambda query, num=10: [
            {"url": "https://acme.io/login", "title": "Acme login",
             "snippet": "", "domain": "acme.io", "serp_rank": 1},
            {"url": "https://acme-support.com/login",
             "title": "Acme Support", "snippet": "phishy",
             "domain": "acme-support.com", "serp_rank": 2},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        lambda term, limit=10: [],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.impersonation.judge_finding",
        lambda **kw: Verdict(
            issue=SafetyAlert.ISSUE_IMPERSONATION,
            severity="high",
            detail="typosquat",
        ),
    )
    alerts = ImpersonationAgent().scan(website, _row(website, "impersonation"))
    assert any(a.source_url == "https://acme-support.com/login" for a in alerts)
    # Own-domain result must not be flagged.
    assert not any("acme.io/login" == a.source_url for a in alerts)


# ── Knowledge-base-driven search + grounding ──────────────────────────────

def _kb_source(website, title):
    from apps.rag.models import KnowledgeSource

    return KnowledgeSource.objects.create(
        user=website.user, website=website,
        url=f"https://acme.io/{abs(hash(title))}",
        title=title, status=KnowledgeSource.STATUS_READY,
    )


def test_sentiment_pulse_searches_derived_terms_and_dedupes_urls(monkeypatch):
    website = _website()
    _kb_source(website, "Acme Widget Pro — Docs")

    searched: list[str] = []

    def fake_reddit(term, limit=25):
        searched.append(term)
        # Same post returned for every query — must yield one finding.
        return [{"title": "WidgetPro broke", "snippet": "post",
                 "url": "https://reddit/widget", "subreddit": "r/gadgets",
                 "score": 1, "num_comments": 0, "created_utc": 0.0}]

    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        fake_reddit,
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.x.search_mentions",
        lambda term, limit=25: [],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.judge_finding",
        lambda **kw: Verdict(issue=SafetyAlert.ISSUE_NEGATIVE, severity="medium"),
    )

    agent = SentimentPulseAgent()
    findings = agent.run(website, {})

    # Both the brand term and the KB-derived product term were searched.
    assert "Acme" in searched
    assert "Acme Widget Pro" in searched
    # Identical post URL across queries produced a single finding.
    assert len(findings) == 1
    assert findings[0].extra["brand_term"] == "Acme"
    assert findings[0].extra["derived_term"] is False


def test_sentiment_pulse_judge_grounds_in_knowledge_base(monkeypatch):
    from types import SimpleNamespace

    website = _website()

    hit = SimpleNamespace(
        chunk_id="c1", source_id="s1", source_url="https://acme.io/about",
        section_label="About", text="Acme has served 10k customers.",
        score=0.91,
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.retrieve",
        lambda **kw: [hit],
    )

    captured: dict = {}

    def fake_judge(**kw):
        captured.update(kw)
        return Verdict(issue=SafetyAlert.ISSUE_NEGATIVE, severity="medium")

    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.judge_finding",
        fake_judge,
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        lambda term, limit=25: [
            {"title": "Acme never had customers", "snippet": "claim",
             "url": "https://reddit/claim", "subreddit": "r/biz",
             "score": 1, "num_comments": 0, "created_utc": 0.0},
        ],
    )
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.x.search_mentions",
        lambda term, limit=25: [],
    )

    alerts = SentimentPulseAgent().scan(website, _row(website, "sentiment_pulse"))

    # The judge received the retrieved chunks as ground truth.
    assert captured["ground_truth"][0]["chunk_id"] == "c1"
    # The alert persisted the evidence trail for the UI.
    assert len(alerts) == 1
    assert alerts[0].evidence_chunks[0]["source_url"] == "https://acme.io/about"


def test_sentiment_pulse_judge_survives_retrieval_failure(monkeypatch):
    website = _website()
    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.retrieve",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("rag down")),
    )
    captured: dict = {}

    def fake_judge(**kw):
        captured.update(kw)
        return Verdict(issue=SafetyAlert.ISSUE_NEGATIVE, severity="medium")

    monkeypatch.setattr(
        "apps.brand_vault.services.security.agents.sentiment_pulse.judge_finding",
        fake_judge,
    )
    from apps.brand_vault.services.security.base import Finding

    verdict = SentimentPulseAgent().judge(website, Finding(
        source=SafetyAlert.SOURCE_REDDIT, title="t", snippet="s",
    ))
    assert verdict.issue == SafetyAlert.ISSUE_NEGATIVE
    assert captured["ground_truth"] == []


def test_narrative_watch_searches_derived_terms(monkeypatch):
    website = _website()
    _kb_source(website, "Acme Widget Pro — Docs")

    searched: list[str] = []
    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.trends.rising_related",
        lambda term: [],
    )

    def fake_reddit(term, limit=15):
        searched.append(term)
        return []

    monkeypatch.setattr(
        "apps.brand_vault.services.security.sources.reddit.search_mentions",
        fake_reddit,
    )
    NarrativeWatchAgent().run(website, {})
    assert "Acme" in searched
    assert "Acme Widget Pro" in searched
