"""Citation Celery tasks."""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.citations.tasks.extract_citations_for_result")
def extract_citations_for_result(result_id: str) -> int:
    """Extract and persist citations for one LLMRankingResult."""
    from apps.citations.services.extraction_service import extract_for_result
    try:
        return extract_for_result(result_id)
    except Exception as exc:  # pragma: no cover
        logger.exception("extract_citations_for_result(%s) failed: %s", result_id, exc)
        return 0


@shared_task(name="apps.citations.tasks.compute_source_influence_snapshots")
def compute_source_influence_snapshots(period_days: int = 30) -> int:
    """Daily rollup of citations into SourceInfluenceSnapshot rows.

    Builds a global snapshot per provider, then a per-website snapshot for
    every website that has citations in the window.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.citations.models import Citation
    from apps.citations.services.snapshot_service import (
        compute_for_website,
        compute_global,
    )
    from apps.websites.models import Website

    total = 0
    try:
        total += compute_global(period_days=period_days)
    except Exception as exc:  # pragma: no cover
        logger.exception("compute_global failed: %s", exc)

    cutoff = timezone.now() - timedelta(days=period_days)
    website_ids = (
        Citation.objects.filter(created_at__gte=cutoff)
        .values_list("audit__website_id", flat=True)
        .distinct()
    )
    for website_id in website_ids:
        if not website_id:
            continue
        try:
            website = Website.objects.get(id=website_id)
        except Website.DoesNotExist:
            continue
        try:
            total += compute_for_website(website, period_days=period_days)
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "compute_for_website(%s) failed: %s", website_id, exc
            )
    return total


@shared_task(name="apps.citations.tasks.classify_unknown_domains")
def classify_unknown_domains(limit: int = 100) -> int:
    """Re-visit domains classified as OTHER and try to upgrade them.

    Phase 2 keeps this rule-only; the LLM upgrade path is wired into the
    classifier service so plumbing it here later is a one-line change.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.citations.models import Citation, DomainClassification, SourceClass
    from apps.citations.services.domain_classifier import _rule_classify

    cutoff = timezone.now() - timedelta(days=1)
    apex_domains = (
        Citation.objects.filter(created_at__gte=cutoff, source_class=SourceClass.OTHER)
        .values_list("apex_domain", flat=True)
        .distinct()[:limit]
    )

    upgraded = 0
    for apex in apex_domains:
        if not apex:
            continue
        new_class = _rule_classify(apex, apex)
        if new_class == SourceClass.OTHER:
            continue
        DomainClassification.objects.update_or_create(
            apex_domain=apex,
            defaults={"source_class": new_class, "classified_by": "rules"},
        )
        Citation.objects.filter(apex_domain=apex, source_class=SourceClass.OTHER).update(
            source_class=new_class,
        )
        upgraded += 1
    return upgraded


@shared_task(name="apps.citations.tasks.run_source_scan")
def run_source_scan(scan_id: str) -> str:
    """Execute one Source Intelligence scan (SERP -> read -> sentiment)."""
    from apps.citations.models import SourceScan
    from apps.citations.services.source_scan import run_scan

    try:
        scan = SourceScan.objects.get(pk=scan_id)
    except SourceScan.DoesNotExist:
        logger.warning("run_source_scan: scan %s not found", scan_id)
        return "not_found"
    try:
        run_scan(scan)
        return scan.status
    except Exception as exc:  # pragma: no cover
        logger.exception("run_source_scan(%s) crashed: %s", scan_id, exc)
        scan.refresh_from_db()
        if scan.status not in ("complete", "failed"):
            scan.status = "failed"
            scan.error = str(exc)[:1000]
            scan.save(update_fields=["status", "error", "updated_at"])
        # A crash inside run_scan can leave lanes mid-flight; without this
        # the UI keeps spinning on them even though the scan is dead.
        from apps.citations.services.source_scan import _finalize_stages
        _finalize_stages(scan, reason=str(exc))
        return "failed"


@shared_task(name="apps.citations.tasks.compute_response_attribution")
def compute_response_attribution(attribution_id: str, user_id: str | None = None) -> None:
    """Work out where one AI answer got its information (on demand)."""
    from apps.citations.services.attribution import compute

    compute(attribution_id, user_id)
