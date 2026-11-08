"""What is set up on this workspace, in words a customer can act on.

Feeds GET /api/v1/agent/status/: the AI engines with whether each has its
key, the Google results provider, whether the research agents can run and
why not, and one ``setup`` block of issues. Customers see plain text
("ChatGPT isn't set up on this workspace"); staff, and everyone while
DEBUG is on, also see the exact setting (OPENAI_API_KEY), so whoever can
fix it knows what to set.

Every value is read from settings, the agent process heartbeat or the
usage ledger. Nothing is assumed: an engine without a key is reported as
not set up and is skipped, never imitated.
"""
from __future__ import annotations

from django.conf import settings

SEVERITY_BLOCKING = "blocking"
SEVERITY_LIMITED = "limited"
SEVERITY_INFO = "info"

GOOGLE_SETTINGS = ("SERPAPI_KEY", "SCRAPERAPI_KEY")
# Runs left at or under this share of the limit read as low (no request
# named how many it needs).
LOW_SHARE = 4


def reveal_settings(user) -> bool:
    """Whether ``user`` may see setting names: staff, or a DEBUG server."""
    return bool(getattr(settings, "DEBUG", False)) or bool(getattr(user, "is_staff", False))


def engines(user) -> list[dict]:
    """``[{key, provider, label, configured, key_setting, reason}]`` in
    display order. ``key_setting`` is "" unless the viewer may see setting
    names; ``reason`` is "" or "not_configured"."""
    from apps.agent.tools.research import engine_catalog

    reveal = reveal_settings(user)
    return [
        {
            "key": e["key"],
            "provider": e["provider"],
            "label": e["label"],
            "configured": e["configured"],
            "key_setting": e["key_setting"] if reveal else "",
            "reason": "" if e["configured"] else "not_configured",
        }
        for e in engine_catalog()
    ]


def google(user) -> dict:
    """``{configured, provider, label, key_settings, reliable_pages}``."""
    from apps.citations.services import serp_google

    which = serp_google.provider()
    return {
        "configured": bool(which),
        "provider": which,
        "label": serp_google.PROVIDER_LABELS.get(which, ""),
        "key_settings": list(GOOGLE_SETTINGS) if reveal_settings(user) else [],
        "reliable_pages": serp_google.max_reliable_pages() if which else 0,
    }


def _issue(code, severity, subject, title, action, *, engine=None, setting=None,
           reveal=False) -> dict:
    return {
        "code": code,
        "severity": severity,
        "subject": subject,
        "engine": engine,
        "setting": setting if reveal else None,
        "title": title,
        "action": action,
    }


def research(*, conf, configured: bool, live: bool, allowance_exceeded: bool,
             engine_rows: list[dict], google_block: dict, free: bool) -> dict:
    """Whether the research agents can run now: ``{available, reason,
    message}``; ``reason`` is the first issue code that stops them."""
    checks = (
        (not conf.enabled, "agent_disabled", "The AI agent is turned off on this workspace."),
        (not configured, "agent_not_configured", "The AI agent isn't set up on this workspace."),
        (not live, "agent_unavailable", "The AI agent isn't running right now."),
        (not conf.swarm_enabled, "research_disabled",
         "The research agents are turned off on this workspace."),
        (allowance_exceeded, "allowance_exceeded",
         "This month's free AI allowance is used up." if free
         else "This month's AI allowance is used up."),
        (not google_block["configured"] and not any(e["configured"] for e in engine_rows),
         "nothing_to_measure",
         "Neither Google results nor any AI engine is set up on this workspace."),
    )
    for failed, code, message in checks:
        if failed:
            return {"available": False, "reason": code, "message": message}
    return {"available": True, "reason": None, "message": ""}


def setup(user, *, conf, configured: bool, live: bool, engine_rows: list[dict],
          google_block: dict, runs: dict, allowance: dict, runs_needed: int | None = None) -> dict:
    """``{ready, issues: [{code, severity, subject, engine, setting, title,
    action}]}``. ``ready`` is False while any issue is blocking."""
    from apps.agent.tools.research import engine_catalog

    reveal = reveal_settings(user)
    free = not runs.get("unlimited", True)
    issues = []
    if not conf.enabled:
        issues.append(_issue(
            "agent_disabled", SEVERITY_BLOCKING, "agent",
            "The AI agent is turned off on this workspace.",
            "Ask your workspace admin to turn it on.", setting="AGENT_ENABLED", reveal=reveal))
    if not configured:
        issues.append(_issue(
            "agent_not_configured", SEVERITY_BLOCKING, "agent",
            "The AI agent isn't set up on this workspace.",
            "Ask your workspace admin to add the Anthropic API key.",
            setting="ANTHROPIC_API_KEY", reveal=reveal))
    if conf.enabled and configured and not live:
        issues.append(_issue(
            "agent_unavailable", SEVERITY_BLOCKING, "agent",
            "The AI agent isn't running right now.",
            "Try again in a minute. If it stays down, contact support."))
    if not conf.swarm_enabled:
        issues.append(_issue(
            "research_disabled", SEVERITY_BLOCKING, "agent",
            "The research agents are turned off on this workspace.",
            "Ask your workspace admin to turn them on.",
            setting="AGENT_SWARM_ENABLED", reveal=reveal))

    settings_by_key = {e["key"]: e["key_setting"] for e in engine_catalog()}
    for e in engine_rows:
        if e["configured"]:
            continue
        issues.append(_issue(
            "engine_not_configured", SEVERITY_LIMITED, "engine",
            f"{e['label']} isn't set up on this workspace.",
            f"{e['label']} is skipped; the other engines still answer.",
            engine=e["key"], setting=settings_by_key.get(e["key"]), reveal=reveal))
    any_engine = any(e["configured"] for e in engine_rows)
    if not any_engine:
        issues.append(_issue(
            "no_engines", SEVERITY_LIMITED if google_block["configured"] else SEVERITY_BLOCKING,
            "engine", "No AI engine is set up on this workspace.",
            "AI answers can't be checked until one is added." if google_block["configured"]
            else "Nothing can be measured until an AI engine or Google results are added."))
    if not google_block["configured"]:
        issues.append(_issue(
            "google_not_configured", SEVERITY_LIMITED, "google",
            "Google results aren't set up on this workspace.",
            "Google rankings are skipped; AI answers are still checked." if any_engine
            else "Google rankings are skipped.",
            setting=" or ".join(GOOGLE_SETTINGS), reveal=reveal))
    elif google_block["reliable_pages"] == 1:
        issues.append(_issue(
            "google_page_one_only", SEVERITY_INFO, "google",
            "Google results cover page one only.",
            "Rankings below the first ten results show as not found."))

    if free:
        remaining = runs.get("remaining") or 0
        limit = runs.get("limit") or 0
        if remaining <= 0:
            issues.append(_issue(
                "prompt_runs_exhausted", SEVERITY_LIMITED, "prompt_runs",
                f"You have used this month's {limit} free prompt runs.",
                "AI engines aren't asked until they renew; Google results still work."))
        elif (runs_needed is not None and remaining < runs_needed) or (
            runs_needed is None and remaining <= max(1, limit // LOW_SHARE)
        ):
            issues.append(_issue(
                "prompt_runs_low", SEVERITY_INFO, "prompt_runs",
                f"{remaining} of your {limit} free prompt runs are left this month.",
                "Each prompt answered by one AI engine uses one run."))
    if allowance.get("exceeded"):
        issues.append(_issue(
            "allowance_exceeded", SEVERITY_BLOCKING, "allowance",
            "This month's free AI allowance is used up." if free
            else "This month's AI allowance is used up.",
            "It renews at the start of your next billing month."))

    return {
        "ready": not any(i["severity"] == SEVERITY_BLOCKING for i in issues),
        "issues": issues,
    }
