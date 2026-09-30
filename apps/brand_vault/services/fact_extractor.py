"""LLM-driven extraction of atomic facts from KnowledgeChunks.

Calls Anthropic Claude with a strict JSON schema and persists each
returned triple as a BrandFact. Extraction is gated on
``BRAND_VAULT_EXTRACTION_ENABLED`` so the test suite can opt out without
an API key. When the flag is off the function is a no-op (returns []).

Idempotency: a candidate triple is skipped if a non-superseded BrandFact
with the same (website, subject, predicate, object) already exists, so
re-running extraction on the same chunk doesn't create duplicates.

Quality gates:
- confidence < 0.5 → discarded
- confidence >= 0.9 → status=AUTO (auto-approved)
- otherwise         → status=PENDING (awaiting review)
"""
from __future__ import annotations

import json
import logging
import re

from django.conf import settings

from apps.brand_vault.models import BrandFact, FactStatus
from apps.brand_vault.services.embeddings import embed_text
from apps.brand_vault.services.fact_versioning import record_creation
from core.llm import ClaudeUtility

logger = logging.getLogger("apps")


# Topic tags let downstream consumers filter the fact base. "security"
# and "privacy" facts (certifications, encryption, incident history,
# retention, data sharing) are what the security-perception claim scorer
# checks AI answers against, so the extractor is asked to label them.
FACT_TOPICS: tuple[str, ...] = (
    "security", "privacy", "pricing", "product", "company", "other",
)
SECURITY_FACT_TOPICS: tuple[str, ...] = ("security", "privacy")

EXTRACTION_PROMPT = (
    "Extract atomic facts about the brand from the following text. "
    "Return ONLY a JSON array. Each element has fields: subject (string), "
    "predicate (string, short verb phrase), object (string), confidence "
    "(float between 0 and 1), topic (one of: security, privacy, pricing, "
    "product, company, other). Use topic \"security\" for certifications, "
    "compliance, encryption, access control, incident history and "
    "vulnerability handling; \"privacy\" for data collection, retention, "
    "sharing and deletion. No prose, no markdown. Use [] if no facts.\n\n"
    "Text:\n{text}\n"
)


def _call_llm(text: str, *, website=None) -> list[dict]:
    """Call Claude via the core gateway and parse the JSON array.

    Empty list on any failure — the gateway returns an error envelope
    instead of raising. ``website`` attributes the token spend to the
    tenant in core.ai_tracking.
    """
    result = ClaudeUtility(model="claude-sonnet-4-20250514", max_tokens=1024).query(
        EXTRACTION_PROMPT.format(text=text),
        user=getattr(website, "user", None),
        website=website,
        module="brand_vault",
        role="fact_extraction",
    )
    if not result.succeeded:
        logger.warning("Brand vault extraction LLM call failed: %s", result.error)
        return []
    return _parse_json_array(result.text)


def _parse_json_array(raw: str) -> list[dict]:
    if not raw:
        return []
    # Try direct first, then strip code fences, then regex hunt.
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    bracket = re.search(r"\[.*\]", raw, re.DOTALL)
    if bracket:
        candidates.append(bracket.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            continue
    return []


def _exists(website_id, subject: str, predicate: str, obj: str) -> bool:
    return BrandFact.objects.filter(
        website_id=website_id,
        subject__iexact=subject,
        predicate__iexact=predicate,
        object__iexact=obj,
        version_to__isnull=True,
    ).exists()


def extract_facts_for_chunk(chunk_id: str) -> list[BrandFact]:
    """Run LLM extraction on a single KnowledgeChunk."""
    if not getattr(settings, "BRAND_VAULT_EXTRACTION_ENABLED", True):
        return []
    from apps.rag.models import KnowledgeChunk

    try:
        chunk = KnowledgeChunk.objects.select_related("source", "website").get(id=chunk_id)
    except KnowledgeChunk.DoesNotExist:
        return []

    raw_items = _call_llm(chunk.text or "", website=chunk.website)
    return _persist_items(
        raw_items,
        website=chunk.website,
        source_chunk=chunk,
        source_url=getattr(chunk.source, "url", "") or "",
    )


def _persist_items(items: list[dict], *, website, source_chunk=None, source_url="") -> list[BrandFact]:
    created: list[BrandFact] = []
    for item in items:
        try:
            subject = (item.get("subject") or "").strip()
            predicate = (item.get("predicate") or "").strip()
            obj = (item.get("object") or "").strip()
            confidence = float(item.get("confidence") or 0.0)
        except Exception:
            continue
        if not subject or not predicate or not obj:
            continue
        if confidence < 0.5:
            continue
        if _exists(website.id, subject, predicate, obj):
            continue
        status = FactStatus.AUTO if confidence >= 0.9 else FactStatus.PENDING
        topic = str(item.get("topic") or "").strip().lower()
        if topic not in FACT_TOPICS:
            topic = ""
        fact = BrandFact.objects.create(
            website=website,
            subject=subject[:300],
            predicate=predicate[:200],
            object=obj,
            topic=topic,
            source_chunk=source_chunk,
            source_url=source_url[:1000],
            confidence=max(0.0, min(1.0, confidence)),
            status=status,
        )
        try:
            fact.embedding = embed_text(
                f"{subject} {predicate} {obj}",
                user=getattr(website, "user", None), website=website,
            )
            fact.save(update_fields=["embedding", "updated_at"])
        except Exception:
            pass
        record_creation(fact)
        created.append(fact)
    return created


def extract_facts_for_website(website_id: str, limit: int = 50) -> int:
    """Iterate KnowledgeChunks that have no facts yet, extract from each."""
    from apps.rag.models import KnowledgeChunk

    qs = (
        KnowledgeChunk.objects.filter(website_id=website_id, facts__isnull=True)
        .distinct()
        .order_by("source_id", "chunk_index")[:limit]
    )
    total = 0
    for chunk in qs:
        try:
            total += len(extract_facts_for_chunk(str(chunk.id)))
        except Exception as exc:  # pragma: no cover
            logger.warning("extract_facts_for_chunk(%s) failed: %s", chunk.id, exc)
    return total


# Test/manual hook so callers can persist pre-parsed items without an LLM.
def persist_extracted_items(items: list[dict], *, website, source_chunk=None, source_url=""):
    return _persist_items(items, website=website, source_chunk=source_chunk, source_url=source_url)
