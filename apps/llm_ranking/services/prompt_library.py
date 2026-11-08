"""
Prompt intent metadata for LLM Ranking audits.

Maps a prompt's intent to its funnel stage and to the short rationale shown
under each prompt row. Prompts themselves always come from the user's saved
list; nothing here generates prompt text.
"""
from __future__ import annotations

# Funnel stages for grouping prompts in the UI. Same idea as marketing
# funnel: bottom-of-funnel = direct intent (the user already knows what
# they want), top-of-funnel = problem unaware. Mapping an intent to a
# funnel stage lets the Prompts table group strategically rather than
# by query shape.
FUNNEL_BOTTOM = "bottom"   # user is ready to pick — high-stakes prompts
FUNNEL_MID = "mid"          # solution-aware — comparison and price intent
FUNNEL_TOP = "top"          # awareness / editorial / thought leadership
FUNNEL_NICHE = "niche"      # long-tail, persona-specific, custom

INTENT_FUNNEL_STAGE: dict[str, str] = {
    "recommendation": FUNNEL_BOTTOM,
    "alternatives":   FUNNEL_BOTTOM,
    "review":         FUNNEL_BOTTOM,
    "comparison":     FUNNEL_MID,
    "use_case":       FUNNEL_MID,
    "category":       FUNNEL_MID,
    "persona":        FUNNEL_TOP,
    "local":          FUNNEL_NICHE,
    "custom":         FUNNEL_NICHE,
}

FUNNEL_STAGE_LABELS: dict[str, str] = {
    FUNNEL_BOTTOM: "Bottom of Funnel — High Intent",
    FUNNEL_MID:    "Mid Funnel — Category & Comparison",
    FUNNEL_TOP:    "Top of Funnel — Awareness",
    FUNNEL_NICHE:  "Niche / Long-Tail",
}

# Per-intent rationale templates. {business_name}, {industry} and
# {use_case} are interpolated at generate time. Plain text — these
# render under each prompt row in the UI to coach the user on what
# the prompt actually tests.
INTENT_RATIONALE: dict[str, str] = {
    "recommendation": (
        "Direct discovery query. If {business_name} doesn't surface here, "
        "nothing else in the prompt run matters — this is the prompt your buyers "
        "actually run."
    ),
    "alternatives": (
        "Competitor-comparison prompt. Tests whether AI places {business_name} "
        "in the same set as the incumbents in {industry}."
    ),
    "review": (
        "Brand-name trust query — the first thing someone searches after "
        "hearing about {business_name}. A weak answer here costs you the "
        "decision; aim for an accurate, positive description."
    ),
    "comparison": (
        "Side-by-side comparison intent. Tests whether AI knows you exist "
        "alongside the {industry} alternatives buyers shortlist."
    ),
    "use_case": (
        "Problem-aware but not solution-aware. Mirrors the use case "
        "{business_name} solves; surfacing here means the AI understands "
        "your category, not just your name."
    ),
    "category": (
        "Category-bucket query. Tests whether AI associates {business_name} "
        "with the broader {industry} space."
    ),
    "persona": (
        "Editorial / listicle prompt — the kind a journalist or analyst "
        "would write. Tests your brand awareness and PR footprint."
    ),
    "local": (
        "Geographic intent. Tests local SEO signals and place-based "
        "mentions for {business_name}."
    ),
    "custom": (
        "Custom prompt — tests a specific phrasing or persona you supplied."
    ),
}


def rationale_for(intent: str, *, business_name: str = "your brand",
                  industry: str = "this category", use_case: str = "") -> str:
    """Render the rationale for a prompt of the given intent."""
    template = INTENT_RATIONALE.get(intent, INTENT_RATIONALE["custom"])
    try:
        return template.format(
            business_name=business_name or "your brand",
            industry=industry or "this category",
            use_case=use_case or industry or "this category",
        )
    except (KeyError, IndexError):
        return template


def funnel_stage_for(intent: str) -> str:
    return INTENT_FUNNEL_STAGE.get(intent, FUNNEL_NICHE)
