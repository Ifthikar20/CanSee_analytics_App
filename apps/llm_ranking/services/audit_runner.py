"""Saved-prompt source for LLM ranking audits.

Audits run exactly the prompts the user curated on the Prompts page.
Nothing is generated: an account with no saved prompts gets a refusal
pointing at the Prompts page, not an invented prompt set.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("apps")


class NoSavedPromptsError(Exception):
    """Raised when a website has no saved prompts to audit."""


def gather_saved_prompts(website, user) -> list[dict]:
    """Materialise the website's saved prompt list into runner shape.

    Reads ``BrandPrompt`` rows (the Prompts-page list), fills
    ``{{ variables }}`` via the prompt-library resolver, and returns the
    dict shape the ranking runner consumes. The runner reads ``text``
    (and ``type`` cosmetically); ``prompt_id`` is carried so results can
    link ``source_prompt`` directly — variable-filled text won't
    hash-match ``Prompt.text``, so the text resolver can't do it.

    Raises ``NoSavedPromptsError`` when the list is empty. Callers turn
    that into a refusal that names the fix (add prompts), never into a
    generated substitute.
    """
    from apps.prompt_library.models import BrandPrompt
    from apps.prompt_library.services.variable_resolver import resolve_for_website
    from core.utils.constants import max_prompts_for_user

    rows = (
        BrandPrompt.objects.filter(
            website=website, prompt__is_active=True, is_archived=False,
        )
        .exclude(prompt__rejections__website=website)
        .select_related("prompt")
        .order_by("-created_at")
    )

    items: list[dict] = []
    seen: set[str] = set()
    for bp in rows:
        prompt = bp.prompt
        filled, missing = resolve_for_website(website, prompt)
        text = (filled or "").strip()
        if not text:
            continue
        if missing:
            logger.debug(
                "Saved prompt %s has unresolved variables %s", prompt.id, missing,
            )
        # Autoseed can create near-duplicate Prompt rows; one text, one run.
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "text": text,
            "type": prompt.intent_bucket or "custom",
            "prompt_id": str(prompt.id),
            "source_label": "Saved",
            "tags": list(bp.tags or []),
        })

    if not items:
        raise NoSavedPromptsError(
            f"Website {website.id} has no saved prompts to audit.",
        )

    cap = max_prompts_for_user(user)
    if len(items) > cap:
        logger.warning(
            "Saved prompt list for website %s truncated to plan cap: "
            "keeping %s most recent of %s.",
            website.id, cap, len(items),
        )
        items = items[:cap]
    return items

