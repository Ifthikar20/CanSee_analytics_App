"""Celery tasks for the brand_vault app."""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.brand_vault.tasks.extract_facts_for_website")
def extract_facts_for_website(website_id: str, limit: int = 50) -> int:
    from apps.brand_vault.services.fact_extractor import extract_facts_for_website as _impl
    try:
        return _impl(website_id, limit=limit)
    except Exception as exc:  # pragma: no cover
        logger.exception("brand_vault.extract_facts_for_website(%s) failed: %s", website_id, exc)
        return 0


@shared_task(name="apps.brand_vault.tasks.audit_recent_results")
def audit_recent_results(website_id: str, audit_id: str | None = None) -> int:
    """Run the Brand Security response auditor for one website.

    Dispatched after a prompt crawl completes (the audit-completion path
    calls the auditor inline; crawls go async so judge latency never
    blocks a crawl). Returns the number of alerts created.
    """
    from apps.brand_vault.services.security.response_auditor import (
        audit_results_for_website,
    )
    from apps.llm_ranking.models import LLMRankingAudit
    from apps.websites.models import Website

    try:
        website = Website.objects.get(id=website_id)
        audit = None
        if audit_id:
            audit = LLMRankingAudit.objects.filter(
                id=audit_id, website=website,
            ).first()
        return audit_results_for_website(website, audit=audit)
    except Exception as exc:  # pragma: no cover
        logger.exception("brand_vault.audit_recent_results(%s) failed: %s", website_id, exc)
        return 0


@shared_task(name="apps.brand_vault.tasks.scan_unaudited_responses")
def scan_unaudited_responses(batch: int = 50) -> int:
    """Catch-up sweep: audit responses no hook has scanned yet.

    Uses BrandSecurityConfig.last_response_scan_at as a per-website
    watermark rather than looking for "results without alerts" — a clean
    result never gets an alert and would be rescanned forever. The
    watermark overlaps 15 minutes backwards per run; the auditor's
    idempotency makes the overlap free. Hourly via beat.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.brand_vault.models import BrandSecurityConfig
    from apps.brand_vault.services.security.response_auditor import (
        audit_results_for_website,
    )
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
    from apps.websites.models import Website

    now = timezone.now()
    default_horizon = now - timedelta(hours=48)
    completed = LLMRankingAudit.STATUS_COMPLETED
    scanned = 0

    website_ids = (
        LLMRankingResult.objects
        .filter(
            created_at__gte=default_horizon,
            query_succeeded=True,
            audit__status=completed,
        )
        .values_list("audit__website_id", flat=True)
        .distinct()
    )
    for website_id in website_ids[: batch * 4]:
        if scanned >= batch:
            break
        try:
            website = Website.objects.get(id=website_id)
            config, _ = BrandSecurityConfig.objects.get_or_create(website=website)
            watermark = config.last_response_scan_at or default_horizon
            has_new = (
                LLMRankingResult.objects
                .filter(
                    audit__website=website,
                    audit__status=completed,
                    query_succeeded=True,
                    created_at__gte=watermark,
                )
                .exists()
            )
            if not has_new:
                continue
            audit_results_for_website(website, since=watermark - timedelta(minutes=15))
            config.last_response_scan_at = now
            config.save(update_fields=["last_response_scan_at", "updated_at"])
            scanned += 1
        except Exception as exc:  # pragma: no cover
            logger.exception("scan_unaudited_responses(%s) failed: %s", website_id, exc)
    return scanned


@shared_task(name="apps.brand_vault.tasks.analyze_alignment_for_result")
def analyze_alignment_for_result(result_id: str) -> str:
    """Compute + persist brand alignment for one LLMRankingResult.

    Dispatched after every successful response write (audit cells, legacy
    audits, prompt crawls). Returns the outcome status string.
    """
    from django.conf import settings

    if not getattr(settings, "CLAIM_VERIFICATION_ENABLED", True):
        return "disabled"

    from apps.brand_vault.services.brand_alignment import compute_alignment
    from apps.llm_ranking.models import LLMRankingResult

    try:
        result = (
            LLMRankingResult.objects
            .select_related("audit__website", "audit__created_by")
            .filter(id=result_id)
            .first()
        )
    except (ValueError, TypeError):
        # Malformed id — the PK is an integer; treat like a deleted row.
        return "missing"
    if result is None:
        return "missing"
    # Self-guard: failure rows and empty answers are never scored, even if
    # a caller dispatches them.
    if not result.query_succeeded or not (result.response_text or "").strip():
        return "skipped"

    try:
        outcome = compute_alignment(result)
    except Exception as exc:  # pragma: no cover — analysis never crashes the queue
        logger.exception("brand alignment failed for result %s: %s", result_id, exc)
        return "error"

    result.alignment_score = outcome.score
    result.alignment_detail = outcome.detail
    result.alignment_model = outcome.model
    # Stamped even on skip statuses so the backfill's staleness cursor
    # never loops over the same rows.
    result.alignment_version = outcome.version
    result.save(update_fields=[
        "alignment_score", "alignment_detail", "alignment_model",
        "alignment_version", "updated_at",
    ])
    return outcome.status


@shared_task(name="apps.brand_vault.tasks.run_brand_pulse")
def run_brand_pulse() -> int:
    """Send the Brand Pulse digest for every enabled website. Daily via beat.

    Gates per pulse: weekly pulses only fire on Mondays (mirroring the
    growth-report convention), and a pulse whose last digest is recent
    (20h for daily, 6d for weekly) is skipped so a double-fired beat or a
    manual re-run never double-sends. Each website is delivered inside its
    own try/except so one failure never stops the sweep.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.brand_vault.models import BrandPulse
    from apps.brand_vault.services import pulse_agent

    now = timezone.now()
    considered = 0
    sent = 0
    pulses = BrandPulse.objects.filter(enabled=True).select_related(
        "website", "website__user",
    )
    for pulse in pulses:
        try:
            if (
                pulse.frequency == BrandPulse.FREQUENCY_WEEKLY
                and now.weekday() != 0
            ):
                continue  # weekly digests go out on Mondays only
            window = (
                timedelta(days=6)
                if pulse.frequency == BrandPulse.FREQUENCY_WEEKLY
                else timedelta(hours=20)
            )
            if pulse.last_digest_at and pulse.last_digest_at > now - window:
                continue  # already sent this period
            considered += 1
            if pulse_agent.deliver_digest(pulse.website):
                sent += 1
        except Exception as exc:  # pragma: no cover — one failure never stops the sweep
            logger.exception(
                "run_brand_pulse failed for website %s: %s", pulse.website_id, exc,
            )
    logger.info(
        "brand pulse sweep: %s considered, %s digests sent", considered, sent,
    )
    return sent


@shared_task(name="apps.brand_vault.tasks.refresh_fact_embeddings")
def refresh_fact_embeddings() -> int:
    """Embed BrandFacts that have no model vector. Daily cron.

    Covers facts stored without a vector (embeddings were unavailable)
    and facts still holding a legacy 32- or 256-dim word-hash vector,
    which are invisible to the 1536-dim semantic space. Does nothing
    until an embedding key is configured. A website whose embedding is
    refused (spent allowance, API error) is skipped until the next run.
    """
    from django.db.models import Q

    from apps.brand_vault.models import BrandFact
    from apps.brand_vault.services.embeddings import embed_text
    from apps.rag.services.embedder import OPENAI_DIM, embeddings_configured

    if not embeddings_configured():
        return 0

    candidates = list(
        BrandFact.objects.filter(Q(embedding__isnull=True) | Q(embedding=[]))
        .select_related("website__user")
        .iterator(chunk_size=200)
    )
    stale = (
        BrandFact.objects.filter(embedding__isnull=False)
        .exclude(embedding=[])
        .select_related("website__user")
        .iterator(chunk_size=200)
    )
    candidates += [f for f in stale if len(f.embedding or []) != OPENAI_DIM]

    count = 0
    refused_websites: set = set()
    for fact in candidates:
        if fact.website_id in refused_websites:
            continue
        vector, _model = embed_text(
            f"{fact.subject} {fact.predicate} {fact.object}",
            website=fact.website,
            # Cron work: spend goes to the website owner, explicitly
            # tagged system so the usage breakdown can tell it apart.
            user=getattr(fact.website, "user", None),
            metadata={"actor": "system"},
        )
        if not vector:
            refused_websites.add(fact.website_id)
            continue
        fact.embedding = vector
        try:
            fact.save(update_fields=["embedding", "updated_at"])
        except Exception as exc:  # pragma: no cover
            logger.warning("refresh_fact_embeddings(%s) failed: %s", fact.id, exc)
            continue
        count += 1
    return count
