"""Celery tasks for the prompt library."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.prompt_library.tasks.mine_daily_prompts")
def mine_daily_prompts(industry_slug: str | None = None) -> dict:
    """Run all configured miners against one or every active industry."""
    from apps.prompt_library.models import Industry
    from apps.prompt_library.services.miner_service import mine_for_industry

    qs = Industry.objects.filter(is_active=True)
    if industry_slug:
        qs = qs.filter(slug=industry_slug)
    summary: dict[str, dict] = {}
    for industry in qs:
        try:
            summary[industry.slug] = mine_for_industry(industry)
        except Exception as exc:  # pragma: no cover
            logger.exception("mine_daily_prompts failed for %s: %s", industry.slug, exc)
            summary[industry.slug] = {"error": str(exc)}
    return summary


@shared_task(name="apps.prompt_library.tasks.compute_demand_scores")
def compute_demand_scores() -> int:
    """Refresh the source score (stored in Prompt.demand_score) for every
    active prompt. The task keeps its registered name for the beat schedule."""
    from apps.prompt_library.services.scoring_service import refresh_all_scores

    return refresh_all_scores()


@shared_task(name="apps.prompt_library.tasks.refresh_effectiveness_scores")
def refresh_effectiveness_scores() -> int:
    """Refresh effectiveness_score for every active prompt.

    Pulls from ``LLMRankingResult`` rows linked via the ``source_prompt``
    foreign key. See :mod:`apps.prompt_library.services.effectiveness`.
    """
    from apps.prompt_library.services.effectiveness import (
        refresh_all_effectiveness_scores,
    )

    return refresh_all_effectiveness_scores()


@shared_task(name="apps.prompt_library.tasks.crawl_prompt_for_website",
             bind=True, max_retries=0)
def crawl_prompt_for_website(
    self, website_id: str, prompt_id: str, only_missing: bool = False,
    acting_user_id: str | None = None, providers: list | None = None,
) -> dict:
    """Fan out + query providers for a single saved prompt.

    Invoked from the Prompt-detail page's 'Run crawler' button and the
    per-prompt schedule dispatcher. We don't retry on failure — the
    crawler service updates the PromptCrawlRun row with whatever it
    managed to capture, so the UI can show partial results plus
    per-provider error notes. ``only_missing`` limits the run to models
    that never answered (the page's silent gap-fill); the default is a
    full re-run of every configured model.

    ``acting_user_id`` is set when a person clicked the button — AI
    spend is attributed to them. The scheduler leaves it None and spend
    goes to the website owner as actor="system". ``providers`` limits the
    crawl to those engines (provider keys).
    """
    from apps.prompt_library.models import Prompt
    from apps.prompt_library.services.prompt_crawler import crawl_prompt
    from apps.websites.models import Website

    try:
        website = Website.objects.get(id=website_id)
        prompt = Prompt.objects.get(id=prompt_id)
    except (Website.DoesNotExist, Prompt.DoesNotExist) as exc:
        logger.warning("crawl_prompt_for_website: %s", exc)
        return {"ok": False, "error": str(exc)}

    acting_user = None
    if acting_user_id:
        from django.contrib.auth import get_user_model

        acting_user = get_user_model().objects.filter(id=acting_user_id).first()

    outcome = crawl_prompt(
        website, prompt, only_missing=only_missing, acting_user=acting_user,
        providers=providers or None,
    )
    return {
        "ok": True,
        "fanouts": len(outcome.fanouts),
        "responses": outcome.responses,
        "errors": outcome.errors,
    }


# ── Periodic per-prompt scheduling ──────────────────────────────────────────

PROMPT_FREQUENCY_DELTAS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}

# A crawl left running past this many minutes is treated as dead (the crawl
# is effectively synchronous), so a fresh scheduled run may proceed.
_STALE_RUN_MINUTES = 10


def _crawl_allowed(owner, website) -> bool:
    """Whether the owner's monthly prompt allowance and prompt runs leave
    room for one scheduled crawl."""
    from apps.billing.services.org_entitlements import (
        monthly_prompt_allowance_for,
        prompts_used_this_month,
    )
    from apps.metering.services import prompt_runs

    allowance = monthly_prompt_allowance_for(owner)
    if allowance >= 0 and prompts_used_this_month(owner) + 1 > allowance:
        return False
    ok, _remaining = prompt_runs.available(owner, website, 1)
    return ok


@shared_task(name="apps.prompt_library.tasks.dispatch_scheduled_prompt_scans")
def dispatch_scheduled_prompt_scans() -> None:
    """Celery Beat task: run any per-prompt schedule whose ``next_run_at``
    has passed, then advance it.

    The per-prompt analogue of
    ``apps.llm_ranking.tasks.dispatch_scheduled_audits``. Runs every 15
    minutes via ``beat_schedule``.

    Resilience guarantees (mirror the audit dispatcher):
      * Skip + bump if the prompt's previous crawl is still in flight, so
        a slow crawl that overruns its cadence never stacks duplicates.
      * The website owner's monthly AI spend cap is checked first — skip +
        bump rather than burn credits while the cap is held. The owner's
        monthly prompt allowance and prompt runs gate the crawl the same
        way (the Google recheck still runs).
      * Track ``consecutive_failures`` and auto-pause once it crosses
        ``auto_pause_threshold``.
    """
    from django.utils import timezone

    from apps.citations.services import serp_google
    from apps.llm_ranking.services.audit_factory import configured_providers
    from apps.prompt_library.models import PromptCrawlRun, PromptSchedule

    now = timezone.now()
    # Without an AI engine key a crawl could only record failures, so the
    # AI side is skipped; the Google recheck still runs when SerpAPI is set.
    engines_configured = bool(configured_providers())
    google_configured = serp_google.is_configured()
    if not engines_configured and not google_configured:
        logger.warning(
            "Skipping scheduled prompt scans: no AI engine or SerpAPI key configured.",
        )
        return

    due = (
        PromptSchedule.objects
        # Archived prompts never run: the UI promises archiving stops them,
        # and each run costs LLM calls plus a paid Google check. Their
        # schedules are left as-is so unarchiving resumes them.
        .filter(
            is_enabled=True,
            next_run_at__lte=now,
            brand_prompt__is_archived=False,
        )
        .select_related(
            "brand_prompt", "brand_prompt__website",
            "brand_prompt__prompt", "created_by",
        )
    )

    for schedule in due:
        try:
            bp = schedule.brand_prompt
            website = bp.website
            prompt = bp.prompt
            # The crawl spends the website owner's allowances (crawl_prompt
            # with no acting user), so the checks read the owner's.
            cap_user = website.user

            # ── 0. Work Autopilot does for this website today ──────────
            # Autopilot asks the engines and reads Google every day on its
            # own; the schedule keeps only what Autopilot does not cover.
            from apps.autopilot.services.settings import covers

            covered = covers(website)
            run_crawl = engines_configured and "engines" not in covered
            run_google = google_configured and "google" not in covered
            if not run_crawl and not run_google:
                logger.info(
                    "Skipping scheduled prompt scan for %s/%s: Autopilot covers it.",
                    website.name, prompt.id,
                )
                _bump_prompt_next_run(schedule, now)
                continue

            # ── 1. Skip if the previous crawl is still in flight ──────
            prev = (
                PromptCrawlRun.objects
                .filter(website=website, prompt=prompt)
                .order_by("-created_at")
                .first()
            )
            # (Only when this schedule crawls: with the engines covered, a
            # crawl in flight is Autopilot's and the Google recheck goes on.)
            if run_crawl and prev and prev.status in (
                PromptCrawlRun.STATUS_PENDING,
                PromptCrawlRun.STATUS_RUNNING,
            ):
                anchor = prev.started_at or prev.created_at
                is_fresh = bool(anchor) and (now - anchor) <= timedelta(
                    minutes=_STALE_RUN_MINUTES
                )
                if is_fresh:
                    logger.info(
                        "Skipping scheduled prompt scan for %s/%s: previous "
                        "crawl %s still %s.",
                        website.name, prompt.id, prev.id, prev.status,
                    )
                    _bump_prompt_next_run(schedule, now)
                    continue

            # ── 2. Spend-cap check (plan-derived allowance) ───────────
            from core.ai_tracking import effective_ai_cap, month_to_date_cost
            cap = effective_ai_cap(cap_user)
            if cap > 0 and month_to_date_cost(cap_user) >= cap:
                logger.warning(
                    "Skipping scheduled prompt scan for %s/%s: user %s at cap.",
                    website.name, prompt.id, cap_user.id,
                )
                _bump_prompt_next_run(schedule, now)
                continue

            # ── 2b. Monthly prompt allowance and prompt runs ──────────
            # Account state, not a schedule failure: the crawl is skipped
            # (the Google recheck still runs) and the schedule advances.
            if run_crawl and not _crawl_allowed(cap_user, website):
                logger.warning(
                    "Skipping scheduled prompt crawl for %s/%s: prompt allowance "
                    "or prompt runs used up.",
                    website.name, prompt.id,
                )
                run_crawl = False
                if not run_google:
                    _bump_prompt_next_run(schedule, now)
                    continue

            # ── 3. Run the single-prompt crawl ────────────────────────
            if run_crawl:
                crawl_prompt_for_website.delay(str(website.id), str(prompt.id))

            # ── 4. Recheck Google on the same cadence (search vs AI gap)
            if run_google:
                from apps.search_gap.tasks import check_google_rank
                check_google_rank.delay(str(bp.id))

            # Advance + reset the failure counter (success path).
            delta = PROMPT_FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
            schedule.last_run_at = now
            schedule.next_run_at = now + delta
            schedule.consecutive_failures = 0
            schedule.last_failure_at = None
            schedule.save(update_fields=[
                "last_run_at", "next_run_at",
                "consecutive_failures", "last_failure_at", "updated_at",
            ])
            logger.info(
                "Scheduled prompt scan queued for %s/%s (schedule=%s, next=%s)",
                website.name, prompt.id, schedule.id, schedule.next_run_at,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to dispatch scheduled prompt scan for schedule %s: %s",
                schedule.id, exc,
            )
            _record_prompt_failure(schedule, now)


def _bump_prompt_next_run(schedule, now) -> None:
    """Advance next_run_at without recording success or failure."""
    delta = PROMPT_FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
    schedule.next_run_at = now + delta
    schedule.save(update_fields=["next_run_at", "updated_at"])


def _record_prompt_failure(schedule, now) -> None:
    """Increment consecutive_failures; auto-pause past the threshold.

    Always advance next_run_at so a transient error can't tight-loop the
    dispatcher.
    """
    delta = PROMPT_FREQUENCY_DELTAS.get(schedule.frequency, timedelta(weeks=1))
    schedule.consecutive_failures = (schedule.consecutive_failures or 0) + 1
    schedule.last_failure_at = now
    schedule.next_run_at = now + delta
    update_fields = [
        "consecutive_failures", "last_failure_at",
        "next_run_at", "updated_at",
    ]
    if schedule.consecutive_failures >= (schedule.auto_pause_threshold or 3):
        schedule.is_enabled = False
        update_fields.append("is_enabled")
        logger.warning(
            "Auto-pausing prompt schedule %s after %d consecutive failures.",
            schedule.id, schedule.consecutive_failures,
        )
    schedule.save(update_fields=update_fields)
