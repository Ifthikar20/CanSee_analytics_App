"""What the agent workspace offers, and whether it can run right now.

``agents()`` is the fixed catalog behind GET agents/. ``status_for(user)``
reports the live configuration (switch, process, model, which AI engines
and Google search have keys, the user's allowance, which writing agents
it can start and whether the research agents are on); every value comes
from settings or the usage ledger, never from a default that pretends.
"""
from __future__ import annotations

from django.core.cache import cache

from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.constants import OUTPUT_KIND_BY_AGENT, AgentKey
from apps.agent.runtime.registry import LIVE_HEARTBEAT_CACHE_KEY

_CATALOG = (
    {
        "key": AgentKey.GAP_ANALYST.value,
        "name": "Gap Analyst",
        "description": (
            "Researches where you rank on Google and in AI answers for the prompts "
            "you track, compares the two and writes a report you can save."
        ),
        "starter": (
            "Research how we rank on Google and in AI answers for our tracked prompts, "
            "and compare the two."
        ),
    },
    {
        "key": AgentKey.LANDING_PAGE.value,
        "name": "Landing Page Agent",
        "description": (
            "Drafts a complete landing page (HTML) for one search intent, written "
            "from your brand context."
        ),
        "starter": "Draft a landing page for the search where we are missing the most.",
    },
    {
        "key": AgentKey.AEO_ARTICLE.value,
        "name": "AEO Article Agent",
        "description": (
            "Writes an answer-first article (Markdown) built to be quoted and cited "
            "by AI engines."
        ),
        "starter": "Write an article that answers the question our buyers ask most.",
    },
    {
        "key": AgentKey.AD_CAMPAIGN.value,
        "name": "Ad Campaign Agent",
        "description": (
            "Builds a Google Search campaign as a sheet: ad groups, keywords, "
            "negatives, ads and sitelinks, checked against Google's limits."
        ),
        "starter": "Build a Google Search ad campaign for our main offering.",
    },
)


def agents() -> list[dict]:
    """The four agents, in display order (contract: GET agents/)."""
    return [{**entry, "output_kind": OUTPUT_KIND_BY_AGENT[entry["key"]]} for entry in _CATALOG]


def agent_name(key: str) -> str:
    """Display name of an agent key ("Landing Page Agent")."""
    for entry in _CATALOG:
        if entry["key"] == key:
            return entry["name"]
    return key


def engines() -> list[dict]:
    """The AI engines the scan pipeline asks, and whether each has a key.

    Same keys, labels and order as the search-gap overview.
    """
    from apps.citations.services.url_analytics import model_key
    from apps.llm_ranking.providers import PROVIDERS
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS, is_configured
    from apps.search_gap.services.overview import ENGINE_ORDER

    return [
        {
            "key": model_key(provider),
            "label": MODEL_LABELS.get(provider, provider),
            "configured": is_configured(PROVIDERS[provider]),
        }
        for provider in ENGINE_ORDER
        if provider in PROVIDERS
    ]


def live_available() -> bool:
    """True in the agent process, or when one has heartbeated recently (the
    agent process refreshes LIVE_HEARTBEAT_CACHE_KEY every few seconds)."""
    if agent_conf().process:
        return True
    try:
        return bool(cache.get(LIVE_HEARTBEAT_CACHE_KEY))
    except Exception:
        # A cache outage must not take status/ down with it.
        return False


def writers(user) -> dict:
    """Whether the user's remaining AI allowance can start each writing
    agent (the check run_agent makes before it starts one), and the least
    it needs, so the workspace can disable a writer before the user pays
    for an orchestrator turn."""
    from apps.agent.subagents.base import OUTPUT_PRICE_PER_TOKEN, _specs, writer_budget

    conf = agent_conf()
    out = {}
    for key, spec in _specs().items():
        max_tokens, minimum, _cap, _spent = writer_budget(user, int(spec.max_tokens(conf)))
        out[key] = {
            "available": max_tokens >= minimum,
            "needs_usd": float(OUTPUT_PRICE_PER_TOKEN * minimum),
        }
    return out


def status_for(user) -> dict:
    """Contract: GET status/. Answers even when the agent is switched off."""
    from apps.citations.services import serp_google
    from apps.metering.services.usage_reader import allowance_status

    conf = agent_conf()
    allowance = allowance_status(user)
    return {
        "enabled": conf.enabled,
        "live_available": live_available(),
        # Whether ANTHROPIC_API_KEY is set; without it no run can start.
        "configured": api_key_configured(),
        "model": conf.model,
        "engines": engines(),
        "serp_configured": serp_google.is_configured(),
        "web_tools_enabled": conf.web_tools_enabled,
        "allowance": {
            "cap_usd": allowance["cap_usd"],
            "spent_usd": allowance["spent_usd"],
            "exceeded": allowance["exceeded"],
            "warning": allowance["warning"],
        },
        "writers": writers(user),
        # The research agents (research_swarm and re-runs).
        "swarm": {
            "enabled": conf.swarm_enabled,
            "max_agents": conf.swarm_max_tasks,
            "worker_model": conf.worker_model,
        },
    }
