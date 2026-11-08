"""Register generated prompts into the prompt library.

Sample prompts drafted for a website are stored as ``Prompt`` rows
(source ``llm_synth``) plus a ``BrandPrompt`` linking each to the
website, so they have ids, show on the Prompts page, and are run by the
next audit like any other saved prompt.

Registration is idempotent — deduped on ``text_hash`` — and never raises:
every failure is logged and swallowed. It links at most the prompts that
still fit the plan's tracked-prompt cap of the website's owner
(apps.billing.services.tracked_prompts); the rest are left out.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("apps")

# Audit intent tags -> the library's coarser IntentBucket choices.
_INTENT_MAP = {
    "recommendation": "category",
    "category": "category",
    "comparison": "comparison",
    "alternatives": "comparison",
    "use_case": "problem",
    "persona": "problem",
    "review": "problem",
    "local": "local",
}


def _bucket_for(intent: str) -> str:
    return _INTENT_MAP.get((intent or "").strip().lower(), "category")


def register_generated_prompts(items: list[dict], website) -> int:
    """Upsert ``items`` (dicts with ``text``/``type``) into the library.

    Returns the number of prompts newly created. Never raises.
    """
    return register_within_plan(items, website)[0]


def register_within_plan(items: list[dict], website) -> tuple[int, int]:
    """``register_generated_prompts`` that also says how many items the plan
    left out: ``(newly created prompts, items left out)``.

    An item already linked to the website (active or archived) needs no room
    under the cap; every other item takes one until the owner's plan is full.
    Never raises.
    """
    if not website or not items:
        return 0, 0
    try:
        from apps.billing.services import tracked_prompts
        from apps.prompt_library.models import BrandPrompt, Prompt
        from apps.prompt_library.services._hash import text_hash
        from apps.prompt_library.services.industry_match import industry_for_website

        industry = industry_for_website(website)
        owner = website.user
        limit = tracked_prompts.limit_for(owner)
        room = None if limit < 0 else max(0, limit - tracked_prompts.used_by(owner))

        created = left_out = 0
        for item in items:
            text = (item.get("text") or "").strip() if isinstance(item, dict) else str(item).strip()
            if not text:
                continue
            digest = text_hash(text)
            prompt = Prompt.objects.filter(text_hash=digest).first()
            linked = prompt is not None and BrandPrompt.objects.filter(
                website=website, prompt=prompt,
            ).exists()
            if not linked and room is not None:
                if room <= 0:
                    left_out += 1
                    continue
                room -= 1
            if prompt is None:
                prompt = Prompt.objects.create(
                    industry=industry,
                    text=text,
                    intent_bucket=_bucket_for(item.get("type") if isinstance(item, dict) else ""),
                    source="llm_synth",
                    excerpt=(item.get("rationale") or "")[:500] if isinstance(item, dict) else "",
                    text_hash=digest,
                    is_active=True,
                    # Generated from this website's content: private to it.
                    owner_website=website,
                )
                created += 1
            # Link to the website so it joins the saved prompt list.
            BrandPrompt.objects.get_or_create(website=website, prompt=prompt)
        return created, left_out
    except Exception as exc:  # pragma: no cover — registration is best-effort
        logger.warning("register_generated_prompts failed: %s", exc)
        return 0, 0
