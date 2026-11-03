"""Rules every writing agent's system prompt shares.

BRAND_RULES ends every writer's system prompt (design/p2_backend.md 11f,
exact text), so each writer reads the six context sections the same way.
"""

BRAND_RULES = (
    "The <brand> block holds the brand's context in six sections; each entry has an id (e1, e2, "
    "...) and the sources it comes from. Company, Audience, Competitors and Offerings are the "
    "facts you may state about the brand. Strategy says what the brand wants to win; use it to "
    "choose the angle. Execution says how to write for the brand (voice, words to use or avoid, "
    "rules); follow it. Never state a fact about the brand that is not in these entries, the "
    "other data blocks or the brief."
)


def with_brand_rules(system_prompt: str) -> str:
    """``system_prompt`` with BRAND_RULES as its last paragraph."""
    return f"{system_prompt.rstrip()}\n\n{BRAND_RULES}\n"
