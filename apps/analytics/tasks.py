import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("apps")

# Rows deleted per statement. Large enough to make progress on a backlog,
# small enough that no single DELETE holds locks long enough to matter.
_BATCH_SIZE = 5_000
# Safety valve for the first run against a large backlog: stop after this many
# batches per table and let the next nightly pass continue. Prevents one
# invocation from running for hours.
_MAX_BATCHES_PER_TABLE = 200


@shared_task(name="apps.analytics.tasks.aggregate_hourly_metrics")
def aggregate_hourly_metrics():
    """Pre-aggregate hourly analytics data."""
    from apps.analytics.services.aggregation_service import AggregationService
    from apps.websites.models import Website

    for website in Website.objects.filter(is_active=True):
        try:
            AggregationService.aggregate_hourly(website_id=str(website.id))
        except Exception as e:
            logger.error(f"Aggregation failed for website {website.id}: {e}")


def _delete_in_batches(queryset, *, label: str, dry_run: bool) -> int:
    """Delete a queryset in chunks. Returns the number of rows affected.

    Chunking is done by primary key rather than with a sliced delete, because
    Django cannot call .delete() on a sliced queryset.
    """
    model = queryset.model
    total = 0

    if dry_run:
        count = queryset.count()
        logger.info("retention: would delete %s %s rows", count, label)
        return count

    for _ in range(_MAX_BATCHES_PER_TABLE):
        pks = list(queryset.values_list("pk", flat=True)[:_BATCH_SIZE])
        if not pks:
            break
        deleted, _detail = model.objects.filter(pk__in=pks).delete()
        # `deleted` counts cascaded rows too, so track the batch size we asked
        # for rather than the cascade total.
        total += len(pks)
        if len(pks) < _BATCH_SIZE:
            break
    else:
        logger.warning(
            "retention: %s hit the batch ceiling (%s rows); "
            "remaining rows will be removed on the next run",
            label,
            total,
        )

    logger.info("retention: deleted %s %s rows", total, label)
    return total


@shared_task(name="apps.analytics.tasks.prune_analytics_events")
def prune_analytics_events(dry_run: bool = False) -> dict:
    """Enforce the retention windows published on /what-we-track.

    Until this existed the six-month figure was only a read clamp
    (analytics_views.EVENT_LOG_RETENTION_DAYS); nothing deleted anything, so
    the public claim that old rows are "automatically purged by a scheduled
    job" was not true.

    Order matters. Visitor cascades to Session and PageEvent, so events are
    removed on their own timestamps first; the Visitor sweep afterwards only
    catches profiles that have themselves been idle past the window, and any
    stragglers it cascades are older than the cutoff by definition.

    Call with dry_run=True to get the counts without deleting.
    """
    from apps.analytics.models import (
        AnalyticsAccessLog,
        LinkClick,
        PageEvent,
        Session,
        Visitor,
    )

    now = timezone.now()
    events_cutoff = now - timedelta(days=settings.ANALYTICS_RETENTION_DAYS)
    access_cutoff = now - timedelta(days=settings.ACCESS_LOG_RETENTION_DAYS)

    results = {
        "dry_run": dry_run,
        "events_cutoff": events_cutoff.isoformat(),
        "access_cutoff": access_cutoff.isoformat(),
    }

    results["page_events"] = _delete_in_batches(
        PageEvent.objects.filter(timestamp__lt=events_cutoff),
        label="PageEvent",
        dry_run=dry_run,
    )
    results["sessions"] = _delete_in_batches(
        Session.objects.filter(started_at__lt=events_cutoff),
        label="Session",
        dry_run=dry_run,
    )
    results["link_clicks"] = _delete_in_batches(
        LinkClick.objects.filter(clicked_at__lt=events_cutoff),
        label="LinkClick",
        dry_run=dry_run,
    )
    # Visitor carries the pseudonymous profile (salted fingerprint, hashed IP,
    # country). It should not outlive the events that justified keeping it.
    results["visitors"] = _delete_in_batches(
        Visitor.objects.filter(last_seen__lt=events_cutoff),
        label="Visitor",
        dry_run=dry_run,
    )
    # Security trail: raw IP and user agent, so a shorter window.
    results["access_logs"] = _delete_in_batches(
        AnalyticsAccessLog.objects.filter(accessed_at__lt=access_cutoff),
        label="AnalyticsAccessLog",
        dry_run=dry_run,
    )

    logger.info("retention: analytics prune complete %s", results)
    return results


@shared_task(name="apps.analytics.tasks.prune_llm_results")
def prune_llm_results(dry_run: bool = False) -> dict:
    """Enforce retention on stored LLM answers, findings and citations.

    Gemini rows are handled separately: when LLM_WEBSEARCH_ENABLED is on,
    Gemini answers are Grounded Results and their terms cap storage at thirty
    days. effective_llm_result_retention_days() resolves that, so the cap and
    the flag cannot drift apart.

    Ask AI answers are LLM answers too: their runs follow the general window
    (the answers go with them) and their Gemini answers the Gemini window,
    counted from when the run was asked.
    """
    from apps.brand_vault.models import SafetyAlert
    from apps.llm_ranking.models import LLMRankingResult
    from apps.prompt_box.models import PromptBoxAnswer, PromptBoxRun
    from core.utils.retention import (
        GEMINI_PROVIDER,
        effective_llm_result_retention_days,
    )

    now = timezone.now()
    general_days = effective_llm_result_retention_days()
    gemini_days = effective_llm_result_retention_days(GEMINI_PROVIDER)

    results = {"dry_run": dry_run, "general_days": general_days, "gemini_days": gemini_days}

    results["llm_results"] = _delete_in_batches(
        LLMRankingResult.objects.filter(
            created_at__lt=now - timedelta(days=general_days)
        ).exclude(provider=GEMINI_PROVIDER),
        label="LLMRankingResult",
        dry_run=dry_run,
    )
    results["llm_results_gemini"] = _delete_in_batches(
        LLMRankingResult.objects.filter(
            provider=GEMINI_PROVIDER,
            created_at__lt=now - timedelta(days=gemini_days),
        ),
        label="LLMRankingResult(gemini)",
        dry_run=dry_run,
    )
    results["prompt_box_runs"] = _delete_in_batches(
        PromptBoxRun.objects.filter(created_at__lt=now - timedelta(days=general_days)),
        label="PromptBoxRun",
        dry_run=dry_run,
    )
    results["prompt_box_answers_gemini"] = _delete_in_batches(
        PromptBoxAnswer.objects.filter(
            provider=GEMINI_PROVIDER,
            run__created_at__lt=now - timedelta(days=gemini_days),
        ),
        label="PromptBoxAnswer(gemini)",
        dry_run=dry_run,
    )
    results["safety_alerts"] = _delete_in_batches(
        SafetyAlert.objects.filter(
            created_at__lt=now - timedelta(days=settings.SAFETY_ALERT_RETENTION_DAYS)
        ),
        label="SafetyAlert",
        dry_run=dry_run,
    )

    logger.info("retention: llm prune complete %s", results)
    return results
