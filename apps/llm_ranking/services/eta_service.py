"""
ETA estimation for LLM ranking audits.

Two flavours:

  • ``estimate_audit_duration(website)`` — look at the last N completed
    audits for this website, return the median per-cell duration times
    the cell count. None when there is no history to measure from.

  • ``schedule_eta(schedule)`` — wraps the above with the schedule's
    next_run_at, the saved-prompt and configured-engine counts the next
    run will use, and whether the schedule is currently in-flight.

Median, not mean — a single 90-second outlier on a flaky API key
would otherwise drag the projection up.
"""
from __future__ import annotations

import statistics

from django.utils import timezone

_HISTORY_WINDOW = 8


def estimate_audit_duration(
    website,
    *,
    n_prompts: int,
    n_providers: int,
) -> float | None:
    """
    Return a projected total duration in seconds for an audit covering
    ``n_prompts × n_providers`` cells against this website.

    Uses the median per-cell duration from the last 8 completed audits
    on this website. Returns None when there are no cells or no history.
    """
    from apps.llm_ranking.models import LLMRankingAudit

    cells = n_prompts * n_providers
    if cells <= 0:
        return None
    history = (
        LLMRankingAudit.objects
        .filter(
            website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            duration_seconds__isnull=False,
            total_queries__gt=0,
        )
        .order_by("-completed_at")
        .values_list("duration_seconds", "total_queries")[:_HISTORY_WINDOW]
    )
    per_cell = [
        float(d) / int(q)
        for d, q in history
        if d and q and float(d) > 0 and int(q) > 0
    ]
    if not per_cell:
        return None
    return cells * statistics.median(per_cell)


def schedule_eta(schedule) -> dict:
    """
    Build the ETA payload for a schedule's next dispatch.

    Returns a dict with:
      - next_run_at        ISO-8601 of the next dispatch
      - seconds_until_run  delta from now to next_run_at (negative if past)
      - n_prompts          saved prompts the next run will ask
      - n_providers        configured engine count
      - estimated_duration_seconds   median-based projection, or None
      - in_flight          true if last_audit is still running
      - in_flight_audit_id last audit's UUID when in_flight
      - in_flight_progress {completed, total} when in_flight
    """
    from apps.llm_ranking.models import LLMRankingAudit
    from apps.llm_ranking.services.audit_factory import configured_providers

    n_prompts = _saved_prompt_count(schedule)
    n_providers = len(configured_providers(schedule.providers or None))

    duration = estimate_audit_duration(
        schedule.website, n_prompts=n_prompts, n_providers=n_providers,
    )

    now = timezone.now()
    seconds_until_run = (
        (schedule.next_run_at - now).total_seconds()
        if schedule.next_run_at else None
    )

    payload: dict = {
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "seconds_until_run": seconds_until_run,
        "n_prompts": n_prompts,
        "n_providers": n_providers,
        "estimated_duration_seconds": round(duration, 1) if duration is not None else None,
        "in_flight": False,
        "in_flight_audit_id": None,
        "in_flight_progress": None,
        "consecutive_failures": schedule.consecutive_failures or 0,
        "is_paused": not schedule.is_enabled,
    }

    prev = schedule.last_audit
    if prev and prev.status in (
        LLMRankingAudit.STATUS_PENDING, LLMRankingAudit.STATUS_RUNNING,
    ):
        payload["in_flight"] = True
        payload["in_flight_audit_id"] = str(prev.id)
        payload["in_flight_progress"] = {
            "completed": prev.queries_completed or 0,
            "total": prev.total_queries or 0,
        }
        # When a run is in flight, surface its remaining-time estimate too.
        cells_left = max(0, (prev.total_queries or 0) - (prev.queries_completed or 0))
        if cells_left and duration is not None:
            per_cell = duration / (n_prompts * n_providers)
            payload["in_flight_seconds_remaining"] = round(cells_left * per_cell, 1)

    return payload


def _saved_prompt_count(schedule) -> int:
    """How many saved prompts the next scheduled run will ask."""
    from apps.llm_ranking.services.audit_runner import (
        NoSavedPromptsError,
        gather_saved_prompts,
    )

    runner = schedule.created_by or schedule.website.user
    try:
        return len(gather_saved_prompts(schedule.website, runner))
    except NoSavedPromptsError:
        return 0
