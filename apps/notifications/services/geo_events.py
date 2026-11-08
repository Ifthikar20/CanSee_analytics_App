"""GEO-side notification producers.

Turn audit results into the in-app notifications the dashboard Recent
Activity feed reads from. The dashboard view in apps/websites already
maps notification.type to a colour, so we reuse the existing audit_complete,
competitor_alert, and milestone types instead of inventing new ones.
"""
from __future__ import annotations

import logging

from apps.notifications.services.notification_service import NotificationService

logger = logging.getLogger("apps")

# Cumulative answered-query thresholds for coverage milestone events.
PROMPT_COVERAGE_MILESTONES = (50, 100, 250, 500, 1000, 2500, 5000, 10000)

# Absolute mention-rate point delta that counts as a meaningful shift between
# two consecutive completed audits on the same website.
VISIBILITY_DELTA_THRESHOLD = 10.0


def record_audit_events(audit) -> None:
    """Fan an audit's completion out to a set of in-app notifications.

    Only completed audits with a measured score qualify: a run where no
    query was answered has nothing to report. Producer-only. Each step is
    independently guarded so a single failure can't block audit save or
    block sibling notifications.
    """
    from apps.llm_ranking.models import LLMRankingAudit

    if (
        audit.status != LLMRankingAudit.STATUS_COMPLETED
        or audit.overall_score is None
        or audit.mention_rate is None
    ):
        return
    for emit in (
        _emit_audit_complete,
        _emit_visibility_change,
        _emit_new_competitors,
        _emit_prompt_coverage_milestone,
    ):
        try:
            emit(audit)
        except Exception as exc:  # never let notifications break audit save
            logger.warning("%s failed for audit %s: %s", emit.__name__, audit.id, exc)


# ── helpers ─────────────────────────────────────────────────────────────────

def _website_name(audit) -> str:
    website = getattr(audit, "website", None)
    return (
        getattr(website, "name", None)
        or getattr(website, "url", None)
        or "your site"
    )


def _previous_audit(audit):
    """The website's previous completed prompt run.

    Only runs from the audit pipeline count (they record ``total_queries``);
    single-prompt scans are not comparable with a full run.
    """
    from apps.llm_ranking.models import LLMRankingAudit
    return (
        LLMRankingAudit.objects
        .filter(
            website_id=audit.website_id,
            status=LLMRankingAudit.STATUS_COMPLETED,
            total_queries__gt=0,
        )
        .exclude(id=audit.id)
        .order_by("-completed_at")
        .first()
    )


def _collect_competitors(audit) -> set[str]:
    from apps.llm_ranking.models import LLMRankingResult
    competitors: set[str] = set()
    for row in LLMRankingResult.objects.filter(audit=audit).values_list(
        "competitors_mentioned", flat=True
    ):
        for entry in row or []:
            # Dict entries since extraction v1 ({name, ...}; v4 adds
            # kind); the str-only check silently emptied this set for
            # every modern audit. Generic terms don't count as new
            # competitors.
            if isinstance(entry, dict):
                if entry.get("kind") == "generic":
                    continue
                name = entry.get("name") or ""
            else:
                name = str(entry)
            if name.strip():
                competitors.add(name.strip())
    return competitors


# ── producers ───────────────────────────────────────────────────────────────

def _emit_audit_complete(audit) -> None:
    NotificationService.create(
        user=audit.created_by,
        notification_type="audit_complete",
        title=f"Prompt run complete — score {audit.overall_score}/100",
        message=(
            f"Your latest prompt run for {_website_name(audit)} finished. "
            f"Overall score {audit.overall_score}/100, "
            f"mention rate {audit.mention_rate:.1f}%."
        ),
        data={
            "audit_id": str(audit.id),
            "website_id": str(audit.website_id),
            "overall_score": audit.overall_score,
            "mention_rate": audit.mention_rate,
        },
        action_url=f"/llm-ranking/{audit.id}/",
    )


def _emit_visibility_change(audit) -> None:
    prev = _previous_audit(audit)
    if prev is None or prev.mention_rate is None:
        return
    delta = audit.mention_rate - prev.mention_rate
    if abs(delta) < VISIBILITY_DELTA_THRESHOLD:
        return
    direction = "up" if delta >= 0 else "down"
    sign = "+" if delta >= 0 else ""
    NotificationService.create(
        user=audit.created_by,
        notification_type="milestone" if delta >= 0 else "competitor_alert",
        title=f"Visibility {direction} {abs(delta):.1f} pts",
        message=(
            f"{_website_name(audit)} moved {sign}{delta:.1f} pts in LLM mention rate "
            f"vs the previous prompt run ({prev.mention_rate:.1f}% to {audit.mention_rate:.1f}%)."
        ),
        data={
            "audit_id": str(audit.id),
            "previous_audit_id": str(prev.id),
            "delta": delta,
        },
        action_url=f"/llm-ranking/{audit.id}/",
    )


def _emit_new_competitors(audit) -> None:
    prev = _previous_audit(audit)
    if prev is None:
        return
    new = sorted(_collect_competitors(audit) - _collect_competitors(prev))
    if not new:
        return
    sample = ", ".join(new[:3])
    extra = f" and {len(new) - 3} more" if len(new) > 3 else ""
    plural = "s" if len(new) != 1 else ""
    NotificationService.create(
        user=audit.created_by,
        notification_type="competitor_alert",
        title=f"{len(new)} new competitor{plural} surfaced",
        message=(
            f"LLMs started mentioning {sample}{extra} in answers for "
            f"{_website_name(audit)}."
        ),
        data={
            "audit_id": str(audit.id),
            "new_competitors": new,
        },
        action_url=f"/llm-ranking/{audit.id}/",
    )


def _emit_prompt_coverage_milestone(audit) -> None:
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
    completed_audits = LLMRankingAudit.objects.filter(
        website_id=audit.website_id,
        status=LLMRankingAudit.STATUS_COMPLETED,
    )
    answered = LLMRankingResult.objects.filter(query_succeeded=True)
    total = answered.filter(audit__in=completed_audits).count()
    this_audit = answered.filter(audit=audit).count()
    previous = total - this_audit
    crossed = next(
        (t for t in PROMPT_COVERAGE_MILESTONES if previous < t <= total),
        None,
    )
    if not crossed:
        return
    NotificationService.create(
        user=audit.created_by,
        notification_type="milestone",
        title=f"{crossed:,} AI answers measured",
        message=(
            f"{_website_name(audit)} has now been measured in "
            f"{crossed:,} AI answers."
        ),
        data={
            "audit_id": str(audit.id),
            "milestone_prompts": crossed,
        },
        action_url=f"/llm-ranking/{audit.id}/",
    )
