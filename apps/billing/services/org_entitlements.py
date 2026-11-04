"""Seat and prompt-allowance resolution for organizations.

The billing model these implement: a seat is one person with access
(member or pending invite), and every seat carries a DEDICATED monthly
prompt allowance — five seats on a 200-prompt package means each of the
five can run 200 prompts a month, not a shared pool of 1000.

Resolution order everywhere: explicit org override (the negotiated
enterprise contract — always binds, even with the paywall dev switch
off) > the org plan's PLAN_LIMITS number > B2C plan number.
"""
from django.utils import timezone

from core.utils.constants import PLAN_LIMITS, Plan, legacy_plan_key


def _org_limits(org) -> dict:
    return PLAN_LIMITS.get(legacy_plan_key(org.plan)) or PLAN_LIMITS[Plan.BUSINESS]


# ── Seats ─────────────────────────────────────────────────────────


def seat_limit_for(org) -> int:
    """Max seats for the org (-1 = unlimited)."""
    if org.seat_limit:
        return int(org.seat_limit)
    raw = _org_limits(org).get("team_members", -1)
    return int(raw) if isinstance(raw, int) else -1


def seats_used(org) -> int:
    """Occupied seats: accepted members + live pending invitations.

    Pending invites count so an admin can't over-invite and have the
    overflow materialize later when links get clicked.
    """
    pending = org.invitations.filter(
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).count()
    return org.members.count() + pending


def seats_block(org) -> dict | None:
    """The session/UI payload: {used, max}. None when unlimited."""
    limit = seat_limit_for(org)
    if limit <= 0:
        return None
    return {"used": seats_used(org), "max": limit}


# ── Monthly prompt allowance ──────────────────────────────────────


def monthly_prompt_allowance_for(user) -> int:
    """Prompts this user may run per calendar month (-1 = unlimited)."""
    from django.conf import settings

    from core.utils.constants import _org_for

    org = _org_for(user)
    if org is not None:
        if org.monthly_prompt_allowance is not None:
            return int(org.monthly_prompt_allowance)
        raw = _org_limits(org).get("monthly_prompts", -1)
        return int(raw) if isinstance(raw, int) else -1

    # B2C: plan-derived; the paywall dev switch lifts plan-derived caps
    # exactly like every other limit resolver.
    if not settings.PAYWALL_ENABLED:
        return -1
    from apps.billing.services.plan_limits import current_plan_for

    limits = PLAN_LIMITS.get(current_plan_for(user)) or PLAN_LIMITS[Plan.FREE]
    raw = limits.get("monthly_prompts", -1)
    return int(raw) if isinstance(raw, int) else -1


def prompts_used_this_month(user) -> int:
    """Prompts queued into audits by this user since the month started.

    Counted at audit creation (len of the prompt list) — a prompt spends
    allowance once regardless of how many providers answer it. Python-side
    sum because ``prompts`` is a JSON list with no portable DB length.

    Autopilot's audits (the ones an AutopilotRunPrompt points to) are left
    out: the number means "prompts you queue yourself", so a daily run never
    uses up the month and refuses the owner's own scans.
    """
    from apps.llm_ranking.models import LLMRankingAudit

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prompt_lists = LLMRankingAudit.objects.filter(
        created_by=user, created_at__gte=month_start, autopilot_rows__isnull=True,
    ).values_list("prompts", flat=True)
    return sum(len(p or []) for p in prompt_lists)


def check_prompt_allowance(user, requested: int) -> None:
    """Raise when queueing ``requested`` prompts would exceed the month.

    The error is client-facing: the SPA shows error.message and can use
    error.details {used, limit, requested} for a meter.
    """
    limit = monthly_prompt_allowance_for(user)
    if limit < 0:
        return
    used = prompts_used_this_month(user)
    if used + max(0, int(requested)) > limit:
        from core.exceptions import CanseeException

        remaining = max(0, limit - used)
        raise CanseeException(
            f"Monthly prompt allowance reached ({used} of {limit} used"
            + (f"; {remaining} left" if remaining else "")
            + "). The allowance resets at the start of next month.",
            code="prompt_allowance_exceeded",
            status_code=403,
            details={"used": used, "limit": limit, "requested": int(requested)},
        )
