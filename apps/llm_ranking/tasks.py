import logging
from datetime import timedelta

from celery import chord, shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.llm_ranking.tasks.run_llm_ranking_audit", bind=True, max_retries=1)
def run_llm_ranking_audit(self, *, audit_id: str) -> None:
    """
    Run a full LLM ranking audit asynchronously.

    Uses a Celery chord to fan out one task per (prompt, provider) cell so:
      - cells run in parallel up to worker concurrency
      - a single cell failure doesn't kill the whole audit
      - retries are bounded per cell, not per audit
      - workers can restart mid-audit without losing committed cells

    The aggregator task is the chord callback — it computes scores once
    every cell has either committed a row or hit max retries.
    """
    from django.db.models import F

    from apps.llm_ranking.models import LLMRankingAudit
    from apps.llm_ranking.services.ranking_service import LLMRankingService

    try:
        # Synchronous prep: enrichment + status flip + persist context.
        # Fast (a few HTTP calls); doing it inside the chord adds no value.
        plan = LLMRankingService.prepare_audit(audit_id=audit_id)
        if not plan:
            # Missing, not runnable (marked failed), or already prepared by
            # another delivery of this task.
            return

        cells = [
            query_provider_prompt_task.s(
                audit_id=audit_id,
                prompt_index=p_idx,
                provider=provider,
            )
            for p_idx in range(len(plan["prompts"]))
            for provider in plan["providers"]
        ]
        if not cells:
            # No work to do: finalise records the run as failed.
            aggregate_audit_results_task.delay([], audit_id=audit_id)
            return

        chord(cells)(aggregate_audit_results_task.s(audit_id=audit_id))
    except Exception as exc:
        logger.error("LLM ranking audit %s failed: %s", audit_id, exc)
        retrying = self.request.retries < self.max_retries
        try:
            # A retry prepares the run again, so it releases the claim
            # (started_at) and leaves the audit pending; the last attempt
            # marks it failed.
            LLMRankingAudit.objects.filter(id=audit_id).update(
                status=(
                    LLMRankingAudit.STATUS_PENDING if retrying
                    else LLMRankingAudit.STATUS_FAILED
                ),
                started_at=None if retrying else F("started_at"),
                error_message=str(exc)[:500],
            )
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=30) from None


@shared_task(
    name="apps.llm_ranking.tasks.query_provider_prompt",
    bind=True, max_retries=2, default_retry_delay=10, acks_late=True,
)
def query_provider_prompt_task(self, *, audit_id: str, prompt_index: int, provider: str) -> dict:
    """
    Run one (prompt, provider) cell of an audit. Idempotent: a duplicate run
    of the same (audit, prompt_index, provider, run_id=0) tuple updates the
    existing row instead of creating a second one. Errors do not propagate
    to the chord — we always commit a row so aggregation can proceed.
    """
    from apps.llm_ranking.services.ranking_service import LLMRankingService
    try:
        return LLMRankingService.run_audit_cell(
            audit_id=audit_id, prompt_index=prompt_index, provider=provider,
        )
    except Exception as exc:
        # Log but don't kill the chord — return a sentinel so aggregation
        # still runs. Retry once on transient errors via Celery's retry.
        logger.warning(
            "Audit %s cell (%d/%s) failed: %s", audit_id, prompt_index, provider, exc,
        )
        try:
            self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {"audit_id": audit_id, "prompt_index": prompt_index,
                    "provider": provider, "error": str(exc)[:200]}


@shared_task(name="apps.llm_ranking.tasks.aggregate_audit_results")
def aggregate_audit_results_task(_cell_results, *, audit_id: str) -> None:
    """
    Chord callback — compute aggregate scores and roll up cost once every
    cell has committed (or exhausted retries). Receives the list of cell
    return values; we ignore them and re-read from the DB for consistency.
    """
    from apps.llm_ranking.services.ranking_service import LLMRankingService
    LLMRankingService.finalise_audit(audit_id=audit_id)


# ── Periodic scheduling ─────────────────────────────────────────────────────


FREQUENCY_DELTAS = {
    "weekly": timedelta(weeks=1),
    "biweekly": timedelta(weeks=2),
    "monthly": timedelta(days=30),
}


@shared_task(name="apps.llm_ranking.tasks.dispatch_scheduled_audits")
def dispatch_scheduled_audits() -> None:
    """
    Celery Beat task: check all enabled LLMRankingSchedule records
    whose ``next_run_at`` has passed, create an audit, and advance
    the schedule.

    Runs every 15 minutes via ``beat_schedule``.

    Resilience guarantees:
      • If the previous audit is still pending/running, do NOT enqueue
        a second one — just bump ``next_run_at`` so we re-check next
        cycle. Avoids stacking duplicates when a slow audit overruns
        its cadence.
      • Track ``consecutive_failures`` and auto-pause the schedule
        once it crosses ``auto_pause_threshold``. Operator can re-
        enable explicitly.
      • Per-user monthly AI spend cap is checked first — skip + advance
        rather than burn credits while the cap is held.
    """
    from django.utils import timezone

    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingSchedule

    now = timezone.now()
    due = LLMRankingSchedule.objects.filter(
        is_enabled=True,
        next_run_at__lte=now,
    ).select_related("website", "last_audit")

    for schedule in due:
        try:
            # Creator may be gone (SET_NULL on departure) — the website
            # owner carries the schedule from then on.
            cap_user = schedule.created_by or schedule.website.user

            # ── 0. Autopilot asks the engines for this website today ──
            from apps.autopilot.services.settings import covers

            if "engines" in covers(schedule.website):
                logger.info(
                    "Skipping scheduled audit for %s: Autopilot covers it.",
                    schedule.website.name,
                )
                _bump_next_run(schedule, now)
                continue

            # ── 1. Skip if previous run still in flight ──────────────
            prev = schedule.last_audit
            if prev and prev.status in (
                LLMRankingAudit.STATUS_PENDING,
                LLMRankingAudit.STATUS_RUNNING,
            ):
                logger.info(
                    "Skipping scheduled audit for %s: previous audit %s "
                    "still %s.", schedule.website.name, prev.id, prev.status,
                )
                _bump_next_run(schedule, now)
                continue

            # ── 2. Spend-cap check (plan-derived allowance) ───────────
            from core.ai_tracking import effective_ai_cap, month_to_date_cost
            cap = effective_ai_cap(cap_user)
            if cap > 0:
                spent = month_to_date_cost(cap_user)
                if spent >= cap:
                    logger.warning(
                        "Skipping scheduled audit for %s: user %s at cap "
                        "($%.2f / $%.2f).",
                        schedule.website.name, cap_user.id, spent, cap,
                    )
                    _bump_next_run(schedule, now)
                    continue

            # ── 3. Saved prompts + filter providers ───────────────────
            # The scheduled run asks exactly what the user curated on the
            # Prompts page. An empty list is user state, not an error:
            # skip and re-schedule rather than record a failure, so the
            # auto-pause gate doesn't punish an unconfigured website.
            from apps.llm_ranking.services.audit_runner import (
                NoSavedPromptsError,
                gather_saved_prompts,
            )
            try:
                prompts = gather_saved_prompts(schedule.website, cap_user)
            except NoSavedPromptsError:
                logger.warning(
                    "Skipping scheduled audit for %s: no saved prompts. "
                    "The user must add prompts on the Prompts page.",
                    schedule.website.name,
                )
                _bump_next_run(schedule, now)
                continue
            # ── 3b. Monthly prompt allowance (per seat / per plan) ────
            # Same skip-and-reschedule treatment as the spend cap: an
            # exhausted allowance is account state, not a schedule failure.
            from apps.billing.services.org_entitlements import (
                monthly_prompt_allowance_for,
                prompts_used_this_month,
            )
            allowance = monthly_prompt_allowance_for(cap_user)
            if allowance >= 0 and (
                prompts_used_this_month(cap_user) + len(prompts) > allowance
            ):
                logger.warning(
                    "Skipping scheduled audit for %s: monthly prompt "
                    "allowance reached (%d).",
                    schedule.website.name, allowance,
                )
                _bump_next_run(schedule, now)
                continue

            # ── 3c. At least one AI engine must have a key ────────────
            # Missing keys are deployment state, not a schedule failure:
            # skip and re-schedule without counting toward auto-pause.
            from apps.llm_ranking.services.audit_factory import (
                configured_providers,
                missing_provider_keys,
            )
            selected_providers = configured_providers(schedule.providers or None)
            if not selected_providers:
                logger.warning(
                    "Skipping scheduled audit for %s: no AI engine configured "
                    "(missing %s).",
                    schedule.website.name,
                    ", ".join(missing_provider_keys(schedule.providers or None)),
                )
                _bump_next_run(schedule, now)
                continue

            audit = LLMRankingAudit.objects.create(
                website=schedule.website,
                created_by=cap_user,
                business_name=schedule.business_name,
                business_description=schedule.business_description,
                industry=schedule.industry,
                location=schedule.location,
                keywords=schedule.keywords,
                prompts=prompts,
                providers_queried=selected_providers,
                prompt_source=LLMRankingAudit.PROMPT_SOURCE_LIBRARY,
            )

            # Queue execution + record the audit on the schedule so the
            # next dispatch cycle can detect it's still in flight.
            run_llm_ranking_audit.delay(audit_id=str(audit.id))

            # Advance + reset failure counter (success path).
            delta = FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
            schedule.last_run_at = now
            schedule.next_run_at = now + delta
            schedule.last_audit = audit
            schedule.consecutive_failures = 0
            schedule.last_failure_at = None
            schedule.save(update_fields=[
                "last_run_at", "next_run_at", "last_audit",
                "consecutive_failures", "last_failure_at", "updated_at",
            ])

            logger.info(
                "Scheduled LLM audit created for %s (schedule=%s, next=%s)",
                schedule.website.name, schedule.id, schedule.next_run_at,
            )

        except Exception as exc:
            logger.error(
                "Failed to dispatch scheduled LLM audit for schedule %s: %s",
                schedule.id, exc,
            )
            _record_failure(schedule, now)


def _bump_next_run(schedule, now) -> None:
    """Advance next_run_at without recording success or failure."""
    delta = FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
    schedule.next_run_at = now + delta
    schedule.save(update_fields=["next_run_at", "updated_at"])


def _record_failure(schedule, now) -> None:
    """
    Increment ``consecutive_failures``; auto-pause the schedule when
    the counter crosses ``auto_pause_threshold``. Always advance
    ``next_run_at`` so a transient error doesn't tight-loop the
    dispatcher.
    """
    schedule.consecutive_failures = (schedule.consecutive_failures or 0) + 1
    schedule.last_failure_at = now
    delta = FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
    schedule.next_run_at = now + delta
    update_fields = [
        "consecutive_failures", "last_failure_at",
        "next_run_at", "updated_at",
    ]
    if schedule.consecutive_failures >= (schedule.auto_pause_threshold or 3):
        schedule.is_enabled = False
        update_fields.append("is_enabled")
        logger.warning(
            "Auto-pausing schedule %s after %d consecutive failures.",
            schedule.id, schedule.consecutive_failures,
        )
    schedule.save(update_fields=update_fields)
