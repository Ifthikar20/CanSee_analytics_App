"""Tenant context sent as ``role: "system"`` messages after a user message.

The first user message of a chat is followed by the project context; a
later message gets a date update only when the date changed since the
last one the chat carries, and a note when the user picked an agent for
that message. They are stored in the user node's wire, so replay sends
the same bytes every time.
"""
from __future__ import annotations

from datetime import date

from apps.agent.constants import AgentKey

AGENT_LABELS = dict(AgentKey.choices)
MAX_COMPETITORS = 12


def _competitors(website) -> list[str]:
    out = []
    for c in website.competitors or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            name = c["name"].strip()
            domain = (c.get("domain") or "").strip()
            out.append(f"{name} ({domain})" if domain else name)
        elif isinstance(c, str) and c.strip():
            out.append(c.strip())
    return out[:MAX_COMPETITORS]


def _agent_line(agent_key: str) -> str:
    label = AGENT_LABELS.get(agent_key, agent_key)
    return f"{label} ({agent_key})"


def brand_context_line(website) -> str:
    """How much context the agents have: active entries per section and
    the suggestions that wait for review (design/p2_backend.md 11f)."""
    from apps.agent.services.context import SECTIONS, counts, suggested_count

    per = counts(website)
    total, waiting = sum(per.values()), suggested_count(website)
    if not total and not waiting:
        return "- Brand context: not set"
    sections = ", ".join(f"{label} {per[key]}" for key, label, _help in SECTIONS)
    return f"- Brand context: {total} entries ({sections}); {waiting} suggestions wait for review"


def project_context(website, *, entry_agent: str, tracked_prompts: int, today: date) -> str:
    competitors = _competitors(website)
    lines = [
        "Project context from CanSee (not written by the user):",
        f"- Project: {website.name}",
        f"- Website: {website.url}",
        f"- Industry: {website.industry or 'not set'}",
        "- Competitors: " + ("; ".join(competitors) if competitors else "none set"),
        f"- Tracked prompts: {tracked_prompts}",
        brand_context_line(website),
        f"- Agent the user started with: {_agent_line(entry_agent)}",
        f"- Today (UTC): {today.isoformat()}",
    ]
    return "\n".join(lines)


def date_update(today: date) -> str:
    return f"Date update. Today (UTC): {today.isoformat()}"


def agent_selected(agent_key: str) -> str:
    return f"For this message the user selected the {_agent_line(agent_key)} agent."
