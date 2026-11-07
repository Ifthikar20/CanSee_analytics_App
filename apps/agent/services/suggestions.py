"""Starter suggestions for an empty chat, built only from real data.

Each suggestion is one of the website's tracked prompts whose Google-vs-AI
gap class (apps.search_gap) points at a concrete next step. A website with
no tracked prompts, or none in those classes, gets an empty list: the UI
then shows no suggestions rather than invented ones.
"""
from __future__ import annotations

from apps.agent.constants import AgentKey

MAX_SUGGESTIONS = 6


def suggestions_for(website) -> list[dict]:
    """[{label, prompt, agent}], biggest gaps first, at most MAX_SUGGESTIONS."""
    from apps.search_gap.services.overview import build_overview
    from apps.search_gap.services.scoring import AI_AHEAD, GOOGLE_AHEAD, INVISIBLE_BOTH

    builders = {
        AI_AHEAD: lambda text: {
            "label": f'Research "{text}"',
            "prompt": f'Research how we rank for "{text}" on Google and AI engines',
            "agent": AgentKey.GAP_ANALYST.value,
        },
        GOOGLE_AHEAD: lambda text: {
            "label": f'Write an article to get cited for "{text}"',
            "prompt": (
                f'Write an article that answers "{text}" so AI engines cite us for it'
            ),
            "agent": AgentKey.AEO_ARTICLE.value,
        },
        INVISIBLE_BOTH: lambda text: {
            "label": f'Draft a landing page for "{text}"',
            "prompt": f'Draft a landing page that targets "{text}"',
            "agent": AgentKey.LANDING_PAGE.value,
        },
    }

    out = []
    # build_overview orders prompts by the size of their gap.
    for row in build_overview(website)["prompts"]:
        text = " ".join(str(row.get("text") or "").split())
        build = builders.get((row.get("gap") or {}).get("class"))
        if text and build:
            out.append(build(text))
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out
