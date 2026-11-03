"""What the agent workspace offers, and whether it can run right now.

``agents()`` is the fixed catalog behind GET agents/. ``status_for(user)``
reports the live configuration (switch, process, model, which AI engines
and Google search have keys, the user's allowance, which writing agents
it can start and whether the research agents are on); every value comes
from settings or the usage ledger, never from a default that pretends.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache

from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.constants import AGENT_TEAMS, OUTPUT_KIND_BY_AGENT, TEAM_LABELS, AgentKey
from apps.agent.runtime.registry import LIVE_HEARTBEAT_CACHE_KEY

# Display order (design/p2_contract.md 12b); the copy is exact.
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
        "key": AgentKey.PAGE_OPTIMIZER.value,
        "name": "Page Optimizer",
        "description": (
            "Rewrites one page of your site against the prompts you track, the searches "
            "engines ran and the crawler audit, and lists every change with the measurement "
            "behind it."
        ),
        "starter": "Optimize our page that engines retrieve but never cite.",
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
        "key": AgentKey.LLMS_TXT.value,
        "name": "llms.txt Agent",
        "description": (
            "Drafts an llms.txt file that points AI tools to the most useful pages of your site."
        ),
        "starter": "Draft an llms.txt file for our site.",
    },
    {
        "key": AgentKey.COMPARISON_PAGE.value,
        "name": "Comparison Page Agent",
        "description": (
            "Writes a comparison page between you and one competitor you track, using only "
            "what each side's own pages say."
        ),
        "starter": "Write a comparison page with our closest competitor.",
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
        "key": AgentKey.AD_CAMPAIGN.value,
        "name": "Ad Campaign Agent",
        "description": (
            "Builds a Google Search campaign as a sheet: ad groups, keywords, "
            "negatives, ads and sitelinks, checked against Google's limits."
        ),
        "starter": "Build a Google Search ad campaign for our main offering.",
    },
    {
        "key": AgentKey.SOCIAL_POSTS.value,
        "name": "Social Posts Agent",
        "description": (
            "Turns a draft or a topic into LinkedIn posts, X posts or an X thread, counted "
            "against each network's limit."
        ),
        "starter": "Write LinkedIn and X posts for our latest article.",
    },
    {
        "key": AgentKey.PR_WRITER.value,
        "name": "PR Agent",
        "description": (
            "Drafts a press release, a media pitch, or correction requests to the publishers "
            "whose pages repeat a claim your evidence contradicts."
        ),
        "starter": "Draft correction requests for an inaccurate claim AI engines repeat.",
    },
    {
        "key": AgentKey.MESSAGING_BRIEF.value,
        "name": "Messaging Agent",
        "description": (
            "Writes a messaging brief from your context: positioning, proof points with their "
            "sources, and how AI engines describe you now."
        ),
        "starter": "Write a messaging brief for our main offering.",
    },
)


def agents() -> list[dict]:
    """The ten agents, in display order (contract: GET agents/), each with
    its teams (the primary one first)."""
    return [
        {**entry, "output_kind": OUTPUT_KIND_BY_AGENT[entry["key"]],
         "teams": [{"key": team, "label": TEAM_LABELS[team]} for team in AGENT_TEAMS[entry["key"]]]}
        for entry in _CATALOG
    ]


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
    agent (the check run_agent makes before it starts one), the least it
    needs, so the workspace can disable a writer before the user pays for
    an orchestrator turn, and the most one new draft's writer request
    costs (its output limit at $20 per MTok plus WRITER_INPUT_USD_MAX)."""
    from apps.agent.subagents.base import (
        OUTPUT_PRICE_PER_TOKEN,
        WRITER_INPUT_USD_MAX,
        _specs,
        writer_budget,
    )

    conf = agent_conf()
    out = {}
    for key, spec in _specs().items():
        limit = int(spec.max_tokens(conf))
        max_tokens, minimum, _cap, _spent = writer_budget(user, limit)
        most = OUTPUT_PRICE_PER_TOKEN * limit + WRITER_INPUT_USD_MAX
        out[key] = {
            "available": max_tokens >= minimum,
            "needs_usd": float(OUTPUT_PRICE_PER_TOKEN * minimum),
            "max_usd": str(most.quantize(Decimal("0.01"))),
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
        # A reply starts no new request once it has used this much.
        "limits": {"run_cost_usd": f"{Decimal(str(conf.run_max_cost_usd)):.2f}"},
        # The research agents (research_swarm and re-runs).
        "swarm": {
            "enabled": conf.swarm_enabled,
            "max_agents": conf.swarm_max_tasks,
            "worker_model": conf.worker_model,
        },
    }
