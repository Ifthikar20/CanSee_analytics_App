"""Per-tenant context for the Ask Cansee assistant.

Two layers, both scoped to the (user, website) the view already resolved:

1. The deterministic account fact pack shared with the Slack/Discord
   "ask" command (traffic + visibility + security + usage headlines).
2. Question-routed provider sections (apps.assistant.services.providers)
   that reach into the rest of the product — per-prompt metrics, the
   saved prompt library, Search Console, citations, audits, the
   knowledge base — loading only the slices the question is actually
   about.

Everything degrades to "" so a failure anywhere still lets the assistant
answer from whatever else it has.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("apps")

# Whole-context ceiling. Generous enough for several subsystems, small
# enough to leave the model room to think and to keep per-question cost
# predictable.
MAX_CONTEXT_CHARS = 9000
# Per-section ceiling so one chatty subsystem cannot starve the others.
MAX_SECTION_CHARS = 2600


@dataclass(frozen=True)
class FactBlock:
    text: str
    # True when a question-routed section carried real figures or rows.
    # The shared headline pack is always present, so it does not count.
    grounded: bool


def _base_fact_block(user, website) -> str:
    """The shared account headline pack (traffic/visibility/security/usage).

    Imported lazily: this module must not pull the notifications Celery
    module at import time.
    """
    try:
        from apps.notifications.tasks import _live_fact_block

        return _live_fact_block(user, website) or ""
    except Exception:
        logger.debug("assistant base fact block unavailable", exc_info=True)
        return ""


def build_fact_block(user, website=None, question: str = "") -> FactBlock:
    """Return the grounding block for one question.

    ``question`` selects which product areas to load; passing "" keeps
    only the headline facts.
    """
    parts: list[str] = []
    data_headers: list[str] = []

    base = _base_fact_block(user, website)
    if base:
        parts.append(base[:MAX_SECTION_CHARS * 2])

    if website is not None and question:
        try:
            from apps.assistant.services.providers import build_sections

            for section in build_sections(user, website, question):
                header = f"=== {section.label} ==="
                body = "\n".join(section.lines)[:MAX_SECTION_CHARS]
                parts.append(f"{header}\n{body}")
                if section.has_data:
                    data_headers.append(header)
        except Exception:
            logger.warning("assistant provider sections failed", exc_info=True)

    if not parts:
        return FactBlock(text="", grounded=False)

    block = (
        "Live Cansee account data for the selected website. Answer from "
        "these figures; never invent numbers. If something the user asked "
        "for is not here, say so and name the Cansee page that has it.\n\n"
        + "\n\n".join(parts)
    )[:MAX_CONTEXT_CHARS]
    # A data section cut off by the ceiling never reached the model.
    grounded = any(header in block for header in data_headers)
    return FactBlock(text=block, grounded=grounded)
