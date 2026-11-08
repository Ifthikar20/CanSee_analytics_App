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


def seat_available(org, *, holding_invitation: bool = False) -> bool:
    """Whether one more person may take a seat in ``org``.

    ``holding_invitation``: the person is accepting an invitation, which
    already reserves its seat (``seats_used`` counts it), so only accepted
    members are compared with the cap. Everyone else (a new invitation,
    domain or SSO auto-join, an operator add) needs a free seat on top of
    the members and the live invitations.

    An org that is over its cap (its plan's seat count was lowered after
    people joined) keeps every member; it just cannot add anyone until it
    is back under the cap.
    """
    cap = seat_limit_for(org)
    if cap <= 0:
        return True
    taken = org.members.count() if holding_invitation else seats_used(org)
    return taken < cap


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

    Prompts of audits deleted this month still count (record_deleted_audit
    keeps them), so deleting an audit never hands its prompts back.
    """
    from apps.llm_ranking.models import LLMRankingAudit

    month_start = _month_start()
    prompt_lists = LLMRankingAudit.objects.filter(
        created_by=user, created_at__gte=month_start, autopilot_rows__isnull=True,
    ).values_list("prompts", flat=True)
    return sum(len(p or []) for p in prompt_lists) + _deleted_prompts(user, month_start)


def _month_start():
    now = timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _deleted_counter_filter(user_id, month_start) -> dict:
    from apps.metering.models import UsageCounter

    return {
        "meter": UsageCounter.METER_DELETED_AUDIT_PROMPTS,
        "account_key": f"user:{user_id}",
        "period_key": month_start.date().isoformat(),
    }


def _deleted_prompts(user, month_start) -> int:
    from apps.metering.models import UsageCounter

    if user is None or not getattr(user, "pk", None):
        return 0
    return (
        UsageCounter.objects.filter(**_deleted_counter_filter(user.pk, month_start))
        .values_list("value", flat=True).first()
        or 0
    )


def record_deleted_audit(audit) -> None:
    """Keep the prompts of an audit about to be deleted on its creator's
    month, when they count toward the monthly prompt allowance (created
    this month, not by Autopilot). Call before ``audit.delete()``."""
    from django.db.models import F

    from apps.metering.models import UsageCounter

    month_start = _month_start()
    count = len(audit.prompts or [])
    if (
        not count or not audit.created_by_id or audit.created_at is None
        or audit.created_at < month_start or audit.autopilot_rows.exists()
    ):
        return
    counter, _ = UsageCounter.objects.get_or_create(
        **_deleted_counter_filter(audit.created_by_id, month_start),
    )
    UsageCounter.objects.filter(pk=counter.pk).update(value=F("value") + count)


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
