"""Phase 2: mirror each app's data into the unified knowledge corpus.

Every adapter turns domain rows into a short natural-language document
and hands it to apps.rag.services.ingest_service.ingest_url with the
right provenance (source_app / source_ref / recorded_at), so the
assistant can retrieve them semantically alongside Brand Input.

TENANT-ISOLATION RULE (non-negotiable):
    An adapter derives ``user`` and ``website`` from the origin row's own
    FKs. It never accepts them as free parameters decoupled from the row,
    and never iterates "all users" writing under one identity. A leak
    here would put one tenant's answers in another tenant's corpus.

Each adapter uses a STABLE synthetic URL as its dedupe key, so re-running
updates in place ((website, url) is unique) and the content-hash
short-circuit in ingest_url skips unchanged text.

All adapters are plain functions: callable synchronously from tests, the
management command, or a Celery task. They never raise for expected
failures - they return the number of sources written.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger("apps")

# Bound each pass so a large account cannot trigger an unbounded
# embedding fan-out (cost + latency).
MAX_LLM_RESPONSES = 25
MAX_ALERTS = 25
MAX_PROMPTS = 50
MAX_DOMAINS = 20


def _enabled() -> bool:
    return bool(getattr(settings, "ASSISTANT_KNOWLEDGE_INGEST_ENABLED", True))


def _ingest(*, user, website, url, title, text, source_app, source_ref,
            recorded_at=None, metadata=None) -> bool:
    """Write one document. Returns True when it was ingested."""
    if not (text or "").strip():
        return False
    from apps.rag.models import KnowledgeSource
    from apps.rag.services.ingest_service import ingest_url

    try:
        ingest_url(
            user=user, website=website, url=url,
            kind=KnowledgeSource.KIND_OTHER,
            title=title[:300],
            text=text,
            source_app=source_app,
            source_ref=str(source_ref),
            metadata=metadata or {},
            recorded_at=recorded_at,
        )
        return True
    except Exception as exc:
        logger.warning("Assistant ingest failed for %s: %s", url, exc)
        return False


# ── llm_response: what the AI models actually said ───────────────────

def ingest_audit_responses(audit_id) -> int:
    """Mirror one completed audit's per-prompt answers."""
    if not _enabled():
        return 0
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult

    # A failed run's answers are not knowledge: only completed audits.
    audit = (
        LLMRankingAudit.objects
        .select_related("website", "created_by")
        .filter(id=audit_id, status=LLMRankingAudit.STATUS_COMPLETED)
        .first()
    )
    if audit is None or audit.website_id is None:
        return 0
    # Identity comes from the audit row itself.
    user = audit.created_by
    website = audit.website
    if user is None:
        return 0

    rows = (
        LLMRankingResult.objects
        .filter(audit=audit, query_succeeded=True)
        .order_by("-created_at")[:MAX_LLM_RESPONSES]
    )
    written = 0
    for r in rows:
        prompt_text = (r.prompt or "").strip()
        answer = (r.response_text or "").strip()
        if not answer:
            continue
        provider = r.provider or "an AI model"
        # Entries are dicts since extraction v1 ({name, ...}; v4 adds
        # kind) with bare strings only on legacy rows — a raw join
        # crashed on every modern row. Generic terms are excluded: the
        # assistant should name real competitor brands only.
        competitors = ", ".join(
            c.get("name", "").strip() if isinstance(c, dict) else str(c).strip()
            for c in (r.competitors_mentioned or [])
            if not (isinstance(c, dict) and c.get("kind") == "generic")
        ).strip(", ")
        lines = [
            f"AI answer from {provider}.",
            f"Prompt asked: {prompt_text}" if prompt_text else "",
            f"Brand mentioned in the answer: {'yes' if r.is_mentioned else 'no'}.",
            f"Mention rank: {r.mention_rank}." if r.mention_rank else "",
            f"Sentiment: {r.sentiment}." if r.sentiment else "",
            f"Competitors mentioned: {competitors}." if competitors else "",
            "",
            answer,
        ]
        ok = _ingest(
            user=user, website=website,
            url=f"llmres://{audit.id}/{r.id}",
            title=f"{provider} answer: {prompt_text[:80]}" if prompt_text
            else f"{provider} answer",
            text="\n".join(x for x in lines if x),
            source_app="llm_response",
            source_ref=str(r.id),
            recorded_at=getattr(r, "created_at", None),
            metadata={
                "provider": provider,
                "audit_id": str(audit.id),
                "prompt": prompt_text[:300],
                "is_mentioned": bool(r.is_mentioned),
            },
        )
        written += 1 if ok else 0
    return written


# ── security_alert: brand-security findings ──────────────────────────

def ingest_security_alerts(website_id) -> int:
    """Mirror a website's open brand-security findings."""
    if not _enabled():
        return 0
    from apps.brand_vault.models import SafetyAlert

    rows = (
        SafetyAlert.objects
        .select_related("website", "website__user")
        .filter(website_id=website_id, status=SafetyAlert.STATUS_OPEN)
        .order_by("-last_seen_at")[:MAX_ALERTS]
    )
    written = 0
    for a in rows:
        website = a.website
        user = getattr(website, "user", None)
        if user is None:
            continue
        lines = [
            f"Brand security finding {a.reference} ({a.severity} severity).",
            f"Issue type: {a.get_issue_display()}.",
            f"Seen on: {a.model}." if a.model else "",
            f"Prompt: {a.prompt_text}" if a.prompt_text else "",
            f"Detail: {a.detail}" if a.detail else "",
            f"Evidence: {a.snippet}" if a.snippet else "",
            f"Times seen: {a.occurrence_count}.",
        ]
        ok = _ingest(
            user=user, website=website,
            url=f"secalert://{website.id}/{a.reference}",
            title=f"Security finding {a.reference}: {a.get_issue_display()}",
            text="\n".join(x for x in lines if x),
            source_app="security_alert",
            source_ref=str(a.id),
            recorded_at=getattr(a, "last_seen_at", None) or a.created_at,
            metadata={
                "severity": a.severity,
                "issue": a.issue,
                "reference": a.reference,
            },
        )
        written += 1 if ok else 0
    return written


# ── prompt_notes: the account's saved prompts ────────────────────────

def ingest_saved_prompts(website_id) -> int:
    """Mirror the website's saved prompt library as ONE document.

    A single roll-up beats one document per prompt: "what are my recent
    prompts" is a list question, and one source keeps the corpus small.
    """
    if not _enabled():
        return 0
    from apps.prompt_library.models import BrandPrompt

    rows = (
        BrandPrompt.objects
        .select_related("prompt", "website", "website__user")
        .filter(website_id=website_id, is_archived=False)
        .order_by("-created_at")[:MAX_PROMPTS]
    )
    rows = list(rows)
    if not rows:
        return 0
    website = rows[0].website
    user = getattr(website, "user", None)
    if user is None:
        return 0

    lines = ["Saved prompts for this website, most recently added first:"]
    for idx, bp in enumerate(rows, start=1):
        text = (getattr(bp.prompt, "text", "") or "").strip()
        if not text:
            continue
        added = bp.created_at.strftime("%Y-%m-%d") if bp.created_at else ""
        tags = ", ".join(bp.tags or [])
        suffix = f" (added {added}" + (f"; tags: {tags}" if tags else "") + ")"
        lines.append(f"{idx}. {text}{suffix}")

    ok = _ingest(
        user=user, website=website,
        url=f"promptnote://{website.id}/saved",
        title="Saved prompts",
        text="\n".join(lines),
        source_app="prompt_notes",
        source_ref=str(website.id),
        recorded_at=rows[0].created_at,
        metadata={"count": len(rows)},
    )
    return 1 if ok else 0


# ── citations: which domains the models cite ─────────────────────────

def ingest_citation_domains(website_id) -> int:
    """Mirror the website's most-cited source domains as one document."""
    if not _enabled():
        return 0
    from apps.citations.models import SourceInfluenceSnapshot
    from apps.websites.models import Website

    website = Website.objects.select_related("user").filter(id=website_id).first()
    if website is None or getattr(website, "user", None) is None:
        return 0

    snap = (
        SourceInfluenceSnapshot.objects
        .filter(website_id=website_id)
        .order_by("-period_end")
        .first()
    )
    if snap is None:
        return 0
    domains = (snap.top_domains or [])[:MAX_DOMAINS]
    if not domains:
        return 0

    lines = [
        f"Sources cited by AI models for this website "
        f"(provider: {snap.provider or 'all'}), most influential first:",
    ]
    for d in domains:
        if isinstance(d, dict):
            name = d.get("domain") or d.get("name") or ""
            count = d.get("count") or d.get("citations") or ""
            lines.append(f"- {name}" + (f": {count} citations" if count else ""))
        else:
            lines.append(f"- {d}")

    ok = _ingest(
        user=website.user, website=website,
        url=f"cite://{website.id}/top-domains",
        title="Most-cited sources",
        text="\n".join(lines),
        source_app="citations",
        source_ref=str(snap.id),
        recorded_at=getattr(snap, "period_end", None),
        metadata={"provider": snap.provider or "", "count": len(domains)},
    )
    return 1 if ok else 0


# ── Whole-website sweep (used by the backfill command) ───────────────

def sync_website(website) -> dict:
    """Run every website-scoped adapter for one website."""
    from apps.llm_ranking.models import LLMRankingAudit

    counts = {
        "security_alerts": ingest_security_alerts(website.id),
        "saved_prompts": ingest_saved_prompts(website.id),
        "citations": ingest_citation_domains(website.id),
        "llm_responses": 0,
    }
    latest = (
        LLMRankingAudit.objects
        .filter(website=website, status=LLMRankingAudit.STATUS_COMPLETED)
        .order_by("-created_at")
        .first()
    )
    if latest is not None:
        counts["llm_responses"] = ingest_audit_responses(latest.id)
    return counts
