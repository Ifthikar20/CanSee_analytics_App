"""One-off prompt smoke test.

Fills a Prompt's template against a website's variables, fires it at
exactly one provider, and returns shape-compatible signals so the UI
can render the same quality affordances it shows for real audit rows.

This intentionally does not persist a real :class:`LLMRankingResult` —
the result row is ephemeral. Cost is still tracked: the provider's
``_record`` hook writes an :class:`AITokenUsage` row tagged
``role=smoke_test`` so finance reporting captures it.
"""
from __future__ import annotations

import logging
import re
import time

from apps.prompt_library.models import Prompt
from apps.prompt_library.services.variable_resolver import resolve_for_website

logger = logging.getLogger("apps")

URL_RE = re.compile(r"https?://[^\s)\]]+", re.IGNORECASE)


def _count_citations(text: str) -> tuple[int, list[str]]:
    if not text:
        return 0, []
    urls = URL_RE.findall(text)
    # Cap to first 3 distinct URLs for the preview chip row.
    seen: list[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
        if len(seen) >= 3:
            break
    return len(urls), seen


def _was_mentioned(text: str, website) -> bool:
    if not text or website is None:
        return False
    name = (getattr(website, "name", "") or "").strip().lower()
    if not name:
        return False
    return name in text.lower()


def smoke_test_prompt(
    prompt: Prompt,
    website,
    provider: str = "claude",
    *,
    user=None,
) -> dict:
    """Run ``prompt`` against one provider and return a result envelope.

    Returns a dict with ``ok``, ``provider``, ``filled_prompt``,
    ``response_text``, ``mentioned``, ``citations_count``,
    ``citations_sample``, ``latency_ms``, ``cost_usd``, and ``error``.
    ``cost_usd`` is None: the cost is recorded in the usage ledger, not
    computed here.
    """
    filled, missing = resolve_for_website(website, prompt)

    envelope = {
        "ok": False,
        "provider": provider,
        "filled_prompt": filled,
        "missing_variables": missing,
        "response_text": "",
        "mentioned": False,
        "citations_count": 0,
        "citations_sample": [],
        "latency_ms": 0,
        "cost_usd": None,
        "error": "",
    }

    # Audit-side providers only — never DeepSeek for what is effectively a
    # mini-audit. The user picks claude / gpt4 / gemini / perplexity.
    try:
        from apps.llm_ranking.providers import get_provider
    except Exception as exc:  # pragma: no cover
        envelope["error"] = f"provider_import_failed: {exc}"
        return envelope

    instance = get_provider(provider)
    if instance is None:
        envelope["error"] = f"provider_not_configured: {provider}"
        return envelope

    from apps.metering.services import prompt_runs

    t0 = time.monotonic()
    try:
        # One prompt run; with none left the engine is not asked.
        result = prompt_runs.guard(
            lambda: instance.query(
                filled,
                "",
                user=user,
                website=website,
                audit_id="prompt_library_smoke_test",
                role="smoke_test",
            ),
            user=user, website=website, engine=provider, feature="smoke_test",
        )
    except Exception as exc:
        envelope["error"] = str(exc)[:300]
        envelope["latency_ms"] = int((time.monotonic() - t0) * 1000)
        return envelope

    envelope["latency_ms"] = getattr(result, "duration_ms", 0) or int(
        (time.monotonic() - t0) * 1000
    )
    if not getattr(result, "succeeded", False):
        envelope["error"] = getattr(result, "error", "") or "unknown_error"
        return envelope

    text = getattr(result, "text", "") or ""
    citations_count, sample = _count_citations(text)
    envelope.update({
        "ok": True,
        "response_text": text,
        "mentioned": _was_mentioned(text, website),
        "citations_count": citations_count,
        "citations_sample": sample,
    })
    return envelope
