"""
Benchmark claim extractor.

Reads a BenchmarkPack's source material (URL or uploaded markdown),
asks Claude to distil it into atomic factual claims a good LLM answer
about this business should include, and persists them as
BenchmarkClaim rows. Each extraction is one Claude call; results are
capped to keep the scoring step tractable later.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings

from apps.citations.services.content_reader import read_url
from apps.prompt_library.models import BenchmarkClaim, BenchmarkPack
from core.llm import ClaudeUtility

logger = logging.getLogger("apps")

MAX_CLAIMS_PER_PACK = 12
MAX_SOURCE_CHARS = 12_000  # keep the prompt cheap


def extract_pack(pack: BenchmarkPack) -> BenchmarkPack:
    """Populate ``pack.claims`` from the pack's source material.

    Idempotent — clears existing claims before writing so a re-extract
    replaces rather than duplicates. Updates ``pack.status`` end-to-end
    and captures the failure reason in ``pack.extraction_error`` when
    something goes wrong (network fetch, bad LLM output, API key
    missing). Never raises to the caller — the pack is left in
    ``failed`` state instead so the UI can surface it.
    """
    pack.status = BenchmarkPack.STATUS_EXTRACTING
    pack.extraction_error = ""
    pack.save(update_fields=["status", "extraction_error", "updated_at"])

    try:
        source_text = _load_source_text(pack)
    except Exception as exc:
        logger.warning("benchmark_extractor: source load failed for %s: %s", pack.pk, exc)
        return _fail(pack, f"source unavailable: {exc}")

    if not source_text.strip():
        return _fail(pack, "no readable content in source")

    # Kept as a settings check (not gateway-relayed) so the pack surfaces
    # the same distinct "ANTHROPIC_API_KEY not configured" reason as before.
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or ""
    if not api_key:
        return _fail(pack, "ANTHROPIC_API_KEY not configured")

    try:
        claims = _ask_claude_for_claims(
            source_text[:MAX_SOURCE_CHARS], website=pack.website,
        )
    except Exception as exc:
        logger.warning("benchmark_extractor: claude failed for %s: %s", pack.pk, exc)
        return _fail(pack, f"extraction failed: {exc}")

    if not claims:
        return _fail(pack, "no claims produced from source")

    # Idempotent write: replace whatever was there.
    BenchmarkClaim.objects.filter(pack=pack).delete()
    BenchmarkClaim.objects.bulk_create([
        BenchmarkClaim(
            pack=pack,
            category=(c.get("category") or "other").strip().lower()[:16],
            text=(c.get("text") or "")[:500],
            evidence=(c.get("evidence") or "")[:500],
            ordinal=i,
        )
        for i, c in enumerate(claims[:MAX_CLAIMS_PER_PACK])
        if (c.get("text") or "").strip()
    ])

    pack.status = BenchmarkPack.STATUS_READY
    pack.save(update_fields=["status", "updated_at"])
    return pack


def _load_source_text(pack: BenchmarkPack) -> str:
    if pack.source_kind == BenchmarkPack.SOURCE_URL:
        content = read_url(pack.url)
        # Fill title/summary so the pack list can show a friendly card
        # even before extraction lands.
        pack.fetched_title = (content.get("kind") or "").title()
        summary = (content.get("text") or "")[:600]
        pack.fetched_summary = summary
        pack.save(update_fields=["fetched_title", "fetched_summary", "updated_at"])
        return content.get("text") or ""
    return pack.markdown_content or ""


def _ask_claude_for_claims(source_text: str, *, website=None) -> list[dict]:
    """One Claude call via the core gateway. Returns a list of claim dicts.

    Raises on gateway failure so ``extract_pack`` captures the reason in
    ``pack.extraction_error`` (same contract as the old raising SDK call).
    ``website`` attributes the token spend to the tenant in
    core.ai_tracking.
    """
    system = (
        "You extract atomic factual claims from a business's own "
        "reference material. Each claim should be a single self-"
        "contained sentence that a good AI answer about this business "
        "should include. Focus on PRODUCT names, CUSTOMER segments, "
        "PRICING model, FEATURE differentiators, and PROOF points "
        "(customers, awards, numbers). Ignore navigation, marketing "
        "fluff, and unrelated links."
    )
    user_msg = (
        "Source material:\n\n"
        f"{source_text}\n\n"
        "Return a JSON object exactly of the form:\n"
        '{"claims": [\n'
        '  {"category": "product|customer|pricing|feature|proof|other", '
        '"text": "one sentence", "evidence": "optional short quote from source"}\n'
        "]}\n"
        f"Cap the list at {MAX_CLAIMS_PER_PACK} of the most important claims. "
        "Reply with the JSON object only, no prose."
    )

    result = ClaudeUtility(
        model="claude-haiku-4-5-20251001", max_tokens=1500,
    ).query(
        user_msg,
        system_prompt=system,
        user=getattr(website, "user", None),
        website=website,
        module="prompt_library",
        role="benchmark_extraction",
    )
    if not result.succeeded:
        raise RuntimeError(result.error)
    raw = result.text
    # Salvage: Claude sometimes prefixes with ```json ... ```.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Second-chance: strip everything before the first '{' and after
        # the last '}' — Claude sometimes drops a stray sentence.
        first = raw.find("{")
        last = raw.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return []
        parsed = json.loads(raw[first:last + 1])

    return list(parsed.get("claims") or [])


def _fail(pack: BenchmarkPack, reason: str) -> BenchmarkPack:
    pack.status = BenchmarkPack.STATUS_FAILED
    pack.extraction_error = reason[:2000]
    pack.save(update_fields=["status", "extraction_error", "updated_at"])
    return pack
