"""Generate a batch of prompts from a free-form user context.

Calls the synthesis provider (DeepSeek by default; Anthropic when DeepSeek
isn't configured) with a strict-JSON instruction asking for ``count``
diverse, natural-feeling prompts in the style of questions a real person
might ask an AI assistant. Returns a list of envelopes the API layer can
serialise or persist.

Nothing is substituted when generation is impossible: a missing provider
raises ``SynthesisNotConfigured`` and a failed or unusable call raises
``SynthesisFailed``. The model only writes prompt text and picks a style
and intent bucket from fixed lists; it is not asked to estimate
popularity or where a question is asked, and is told not to invent
business names.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from apps.prompt_library.services.template_parser import extract_variables

logger = logging.getLogger("apps")

ALLOWED_STYLES = {
    "story",          # multi-sentence anecdote
    "question",       # direct, single-sentence
    "comparison",     # X vs Y
    "local",          # mentions place / route / time
    "how_to",         # instructional
    "listicle",       # ranking-style
    "recommendation", # asking for picks
    "skeptical",      # doubting a claim
    "discovery",      # 'I just heard / saw...'
    "problem",        # frustrated user trying to fix something
    "verification",   # 'is X legit?'
    "experience",     # first-person 'I tried X'
}
ALLOWED_BUCKETS = {"category", "comparison", "problem", "local"}


class SynthesisNotConfigured(Exception):
    """No synthesis provider has an API key."""

    code = "synthesis_not_configured"


class SynthesisFailed(Exception):
    """The provider call failed or returned nothing usable."""

    code = "synthesis_failed"


_SYSTEM = (
    "You write natural-feeling questions people ask AI assistants like "
    "ChatGPT, Claude, Gemini, and Perplexity in real life. Your prompts "
    "should sound like something a real person typed — first-person, "
    "conversational, sometimes meandering, sometimes terse. They should NOT "
    "sound like SEO listicle headlines (no 'best X 2026', 'top 10 Y').\n\n"
    "Use only the business names, places and details given in the user's "
    "context. Do not invent business names. Do not use placeholder syntax "
    "like '{{ company_name }}' or 'Brand X'.\n\n"
    "Mix tones: skeptical, curious, frustrated, excited, lazy."
)

_USER_TEMPLATE = (
    "Generate {count} DISTINCT prompts inspired by the user context below.\n\n"
    "MIX LENGTHS DELIBERATELY: some short (one sentence, at most 15 words), "
    "some medium (2 sentences, 15-30 words), some long (3-4 sentences, "
    "30-60 words, conversational, with personal context).\n\n"
    "MIX STYLES across the {count} items. Use as many of these styles as "
    "possible: story, question, comparison, local, how_to, recommendation, "
    "skeptical, discovery, problem, verification, experience.\n\n"
    'CONTEXT: """{context_text}"""\n\n'
    "Output strict JSON: a list of {count} objects, each with these keys:\n"
    "  prompt_text    (string — the full prompt text. No placeholders.)\n"
    "  style          (one of: story, question, comparison, local, how_to, "
    "listicle, recommendation, skeptical, discovery, problem, verification, "
    "experience)\n"
    "  intent_bucket  (one of: category, comparison, problem, local)\n\n"
    "Return ONLY the JSON array, no commentary."
)


# Compact instruction for the related-prompt recommender. Kept intentionally
# small (this is a secondary feature) to minimise DeepSeek token spend.
_RELATED_SYSTEM = (
    "You write short, natural questions a real person would ask an AI "
    "assistant. Reply with JSON only."
)

_RELATED_USER_TEMPLATE = (
    'Existing prompt: "{seed}"\n'
    "Write {count} short related questions on the same topic and audience, "
    "each from a different angle. No placeholders, and no business names "
    "that are not in the existing prompt.\n"
    'Return only a JSON array of objects with keys: "prompt_text" (string), '
    '"style" (one of question, comparison, how_to, local, problem), '
    '"intent_bucket" (one of category, comparison, problem, local).'
)


def _strip_fences(text: str) -> str:
    """Drop ```json ... ``` markdown fences if the model added them."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def _extract_json_array(text: str) -> list[Any]:
    """Pull the first JSON array out of text. Attempt a best-effort repair
    when the response is truncated mid-object."""
    if not text:
        return []
    text = _strip_fences(text)
    match = re.search(r"\[.*", text, re.DOTALL)
    if not match:
        return []
    candidate = match.group()
    # Direct parse first.
    try:
        data = json.loads(candidate)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    # Repair attempt: trim to last complete object and close the array.
    last_brace = candidate.rfind("}")
    if last_brace == -1:
        return []
    repaired = candidate[: last_brace + 1] + "]"
    try:
        data = json.loads(repaired)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _normalise_item(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    # Accept either the concrete-prompt schema (prompt_text) or the older
    # template-with-placeholders schema (template_text).
    prompt_text = str(raw.get("prompt_text") or raw.get("template_text") or "").strip()
    if len(prompt_text) < 10:
        return None
    style = str(raw.get("style") or "question").strip().lower()
    if style not in ALLOWED_STYLES:
        style = "question"
    bucket = str(raw.get("intent_bucket") or "category").strip().lower()
    if bucket not in ALLOWED_BUCKETS:
        bucket = "category"
    # Variables are extracted defensively — concrete prompts won't have any,
    # but if the model slips in a {{ slot }} we still capture it for storage.
    template_variables = extract_variables(prompt_text)
    # Word + length band — server-side so the frontend filter is just a
    # simple equality check.
    word_count = len([w for w in prompt_text.split() if w])
    if word_count <= 14:
        length_band = "short"
    elif word_count <= 30:
        length_band = "medium"
    else:
        length_band = "long"
    return {
        # Legacy fields kept for storage/compat with the existing Prompt model.
        "template_text": prompt_text,
        "template_variables": template_variables,
        "preview_text": prompt_text,
        "prompt_text": prompt_text,
        "style": style,
        "intent_bucket": bucket,
        "word_count": word_count,
        "length_band": length_band,
    }


def _query(provider, user_prompt: str, system: str, *, user, audit_id: str, role: str) -> str:
    """Run one synthesis call and return its text, or raise SynthesisFailed."""
    try:
        result = provider.query(
            user_prompt,
            system,
            user=user,
            website=None,
            audit_id=audit_id,
            role=role,
        )
    except Exception as exc:
        logger.warning("%s provider call failed: %s", audit_id, exc)
        raise SynthesisFailed() from exc
    if not getattr(result, "succeeded", False):
        logger.warning("%s provider call unsuccessful: %s", audit_id, getattr(result, "error", ""))
        raise SynthesisFailed()
    return getattr(result, "text", "") or ""


def generate_from_context(
    context_text: str,
    *,
    count: int = 20,
    user=None,
) -> tuple[list[dict], str]:
    """Return (items, provider_name).

    Raises ``SynthesisNotConfigured`` when no synthesis provider has a key
    and ``SynthesisFailed`` when the call fails or yields no usable prompt.
    """
    from apps.llm_ranking.providers import get_synthesis_provider

    context_text = (context_text or "").strip()
    provider = get_synthesis_provider()
    if provider is None:
        raise SynthesisNotConfigured()

    text = _query(
        provider,
        _USER_TEMPLATE.format(count=count, context_text=context_text),
        _SYSTEM,
        user=user,
        audit_id="prompt_library_context_generator",
        role="synthesis",
    )
    items: list[dict] = []
    for raw in _extract_json_array(text):
        norm = _normalise_item(raw)
        if norm is not None:
            items.append(norm)
        if len(items) >= count:
            break
    if not items:
        raise SynthesisFailed()
    return items, getattr(provider, "name", "")


def generate_related_prompts(
    seed_prompt: str,
    *,
    count: int = 5,
    user=None,
) -> tuple[list[dict], str]:
    """Return (items, provider_name) of prompts related to ``seed_prompt``.

    Secondary recommender used by the create-prompt flow, kept cheap: it
    pins DeepSeek (falling back to Claude/GPT only when DeepSeek has no
    key) and asks for a small ``count``. Token usage is metered against
    ``user``. Raises ``SynthesisNotConfigured`` / ``SynthesisFailed`` like
    ``generate_from_context``.
    """
    from apps.llm_ranking.providers import get_synthesis_provider

    seed = (seed_prompt or "").strip()
    count = max(1, min(int(count or 5), 10))
    provider = get_synthesis_provider("deepseek")
    if provider is None:
        raise SynthesisNotConfigured()

    text = _query(
        provider,
        _RELATED_USER_TEMPLATE.format(seed=seed[:300], count=count),
        _RELATED_SYSTEM,
        user=user,
        audit_id="prompt_library_related",
        role="recommendation",
    )
    items: list[dict] = []
    for raw in _extract_json_array(text):
        norm = _normalise_item(raw)
        if norm is not None:
            items.append(norm)
        if len(items) >= count:
            break
    if not items:
        raise SynthesisFailed()
    return items, getattr(provider, "name", "")
