"""Feature-gating helpers that resolve a user's plan and check limits."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.utils.constants import PLAN_LIMITS, Plan, SubscriptionStatus

# A TRIALING row is only trusted this long past its trial end. Polar charges
# the first invoice at trial end and emits subscription.active/updated +
# order.paid within minutes; if our webhook endpoint is down Polar redelivers
# with backoff over roughly a day. 24h absorbs both, so a converting customer
# is never flickered to Free by a late webhook, while a failed card (or a dev
# box that can never receive webhooks) buys at most one extra day of Pro.
# Request paths re-verify an ended trial against Polar well before this
# (see polar_billing.reverify_ended_trial); this is the offline safety net.
TRIAL_LAPSE_GRACE = timedelta(hours=24)

# Plans a subscription row grants as themselves. Anything else on an
# access-granting row (a legacy value) resolves through _LEGACY_MAP, else to
# Pro: honour the payment rather than drop a paying account to Free.
PAID_PLANS = (Plan.PROMPTS_100, Plan.PROMPTS_200, Plan.PRO, Plan.BUSINESS)


def trial_end_for(sub):
    """End of the trial window for a TRIALING row, else None.

    Polar sets ``current_period_end == trial_end`` while a subscription is
    trialing (the trial IS the first billing period), so no extra column
    is needed. This accessor is the only place that knows that; swap in a
    dedicated column here if one is ever added.
    """
    if sub is None or getattr(sub, "status", None) != SubscriptionStatus.TRIALING:
        return None
    return getattr(sub, "current_period_end", None)


def trial_ended(sub, now=None) -> bool:
    """True when a TRIALING row's trial end has passed (no grace)."""
    end = trial_end_for(sub)
    return end is not None and end <= (now or timezone.now())


def trial_lapsed(sub, now=None) -> bool:
    """True when a TRIALING row is past its trial end by more than
    TRIAL_LAPSE_GRACE — i.e. the conversion webhook never arrived and the
    row can no longer be trusted to grant anything."""
    end = trial_end_for(sub)
    return end is not None and end + TRIAL_LAPSE_GRACE <= (now or timezone.now())


def plan_for_subscription(sub) -> str:
    """Resolve a Subscription row (or None) to the plan it grants.

    Prefer this over ``current_plan_for`` when you already hold the row
    — ``getattr(user, "subscription")`` caches misses on the instance,
    so a row created or synced mid-request would otherwise be invisible.
    """
    if (
        sub is not None
        and sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
        and not trial_lapsed(sub)
    ):
        plan = _LEGACY_MAP.get(sub.plan, sub.plan)
        if plan in PAID_PLANS:
            return plan
        return Plan.PRO  # active sub on an unrecognized value: honor payment
    return Plan.FREE


def current_plan_for(user) -> str:
    """The plan the account is ACTUALLY on, resolved from subscription
    status — the single source of truth for billing-facing surfaces.

    An active or trialing subscription grants its plan; everything else
    (no subscription, canceled, past_due beyond grace) is the FREE plan.
    The denormalized ``user.plan`` value is deliberately not consulted:
    it defaults to a paid tier and historically leaked paid allowances
    to unsubscribed accounts.
    """
    if user is None:
        return Plan.FREE
    try:
        sub = getattr(user, "subscription", None)
    except Exception:
        sub = None
    return plan_for_subscription(sub)

# Legacy plan → live-tier mapping (Free / Pro $45 self-serve / Business custom)
_LEGACY_MAP = {
    "individual": Plan.PRO,
    "starter": Plan.PRO,
    "growth": Plan.PRO,
    "scale": Plan.BUSINESS,
    "team": Plan.BUSINESS,
    "enterprise": Plan.BUSINESS,
}


def is_paying_subscription(sub) -> bool:
    """Provider-backed payment gate used for paywall routing, on a row.

    ACTIVE always counts; TRIALING counts only when a Polar subscription
    backs it (card on file, auto-converts) and the trial has not lapsed.
    Deliberately stricter than ``plan_for_subscription``, which grants the
    tier for any ACTIVE/TRIALING row.
    """
    if sub is None:
        return False
    if sub.status == SubscriptionStatus.ACTIVE:
        return True
    return bool(
        sub.status == SubscriptionStatus.TRIALING
        and sub.polar_subscription_id
        and not trial_lapsed(sub)
    )


def is_paying(user) -> bool:
    """``is_paying_subscription`` for the user's own row (see the caching
    caveat on ``plan_for_subscription``)."""
    sub = getattr(user, "subscription", None) if user is not None else None
    return is_paying_subscription(sub)


def is_trialing_subscription(sub) -> bool:
    """A live free trial: TRIALING and not lapsed. Labels must say
    "Free trial" here — the card has not been charged yet."""
    return bool(
        sub is not None
        and sub.status == SubscriptionStatus.TRIALING
        and not trial_lapsed(sub)
    )


def tier_for_subscription(sub) -> str | None:
    """The tier the row is FOR (one of PAID_PLANS, legacy names
    normalized), regardless of status — for labels in non-access states
    such as past_due ("Pro plan · payment issue"). None when there is no
    row."""
    if sub is None:
        return None
    plan = _LEGACY_MAP.get(sub.plan, sub.plan)
    return plan if plan in PAID_PLANS else Plan.PRO


def subscription_state(sub) -> dict:
    """The subscription block of the session / billing-overview payloads.

    One builder so every surface labels a trial, a paid plan and a lapsed
    row the same way:
      status               raw row status (or None)
      plan                 resolved ACCESS tier: free, or one of PAID_PLANS
      tier                 subscribed tier regardless of status, or None
      is_paying            paywall gate (see is_paying_subscription)
      is_trialing          live free trial, not yet charged
      trial_end            ISO trial end while trialing, else None
      current_period_end   ISO, or None
      cancel_at_period_end bool
    """

    def _iso(dt):
        return dt.isoformat() if dt else None

    return {
        "status": sub.status if sub is not None else None,
        "plan": str(plan_for_subscription(sub)),
        "tier": tier_for_subscription(sub),
        "is_paying": is_paying_subscription(sub),
        "is_trialing": is_trialing_subscription(sub),
        # Billing cadence ("month" | "year"); None when unknown. Period
        # length can't stand in for this while trialing, so surfaces that
        # offer a monthly/annual switch must read it from here.
        "interval": (getattr(sub, "interval", "") or None) if sub is not None else None,
        "trial_end": _iso(trial_end_for(sub)),
        "current_period_end": _iso(getattr(sub, "current_period_end", None)) if sub is not None else None,
        "cancel_at_period_end": bool(getattr(sub, "cancel_at_period_end", False)) if sub is not None else False,
    }


def org_plan_for(user):
    """The plan the user's organization grants, or None outside an org.

    Enterprise org members inherit the org's manually provisioned plan
    (Business has no self-serve checkout). Mirrors User.effective_plan's
    org branch WITHOUT its personal ``user.plan`` fallback, which defaults
    to a paid tier and leaked paid features to unsubscribed accounts.
    Legacy plan names map to the live tiers.

    None while ORG_FEATURES_ENABLED is off: the master switch keeps every
    account plain B2C (the session and billing payloads show no org then),
    so a membership staged before launch grants nothing until it is on.
    """
    from core.permissions.org import org_features_enabled

    if user is None or not getattr(user, "pk", None):
        return None
    if not org_features_enabled():
        return None
    if not hasattr(user, "org_memberships"):
        return None
    org = getattr(user, "_org_cache", None)
    if org is None:
        # order_by matters: .first() on an unordered queryset gives an
        # arbitrary row, so a user in two orgs would flap between plans.
        membership = (
            user.org_memberships.select_related("organization")
            .order_by("created_at")
            .first()
        )
        if membership is None:
            return None
        org = membership.organization
        user._org_cache = org
    return _LEGACY_MAP.get(org.plan, org.plan)


def _resolve_plan_key(user):
    """Resolve the user's effective plan to a PLAN_LIMITS key."""
    # Paywall off: everyone gets the top tier so no feature or numeric
    # limit blocks them.
    if not settings.PAYWALL_ENABLED:
        return Plan.BUSINESS
    # System callers and anonymous users keep the Pro-tier fallback that
    # the old ``user.plan`` default provided.
    if user is None or not getattr(user, "pk", None):
        return Plan.PRO
    org_plan = org_plan_for(user)
    if org_plan is not None:
        return org_plan
    return current_plan_for(user)


def get_limits(user):
    """Return the PLAN_LIMITS dict for a user's effective plan."""
    plan_key = _resolve_plan_key(user)
    return PLAN_LIMITS.get(plan_key, PLAN_LIMITS[Plan.PRO])


def plan_allows(user, feature: str) -> bool:
    """Whether the user's effective plan includes ``feature``: ``autopilot``
    (the daily run) or ``sms_digest`` (its one text a day). Paywall off,
    every account is on Business and has both."""
    return bool(get_limits(user).get(feature))


def limits_block(user) -> dict:
    """The resolved limits of the billing overview, the plan change answers
    and the session payload (which adds ``seats`` for org members).

    ``tracked_prompts`` is ``{"limit", "used"}`` with -1 for unlimited.
    """
    from apps.billing.services import tracked_prompts

    return {
        "projects": projects_limit_for(user),
        "tracked_prompts": tracked_prompts.capacity(user),
        "autopilot": plan_allows(user, "autopilot"),
        "sms_digest": plan_allows(user, "sms_digest"),
    }


def projects_limit_for(user) -> int:
    """Max projects (websites) the account may have. -1 means unlimited.

    A live free trial is deliberately capped at the FREE tier's project
    count: trialing grants the Pro feature set to explore, but the full
    five-project allowance only unlocks once the card is charged and the
    subscription is ACTIVE. Everything else follows the effective plan
    (including the paywall-off and org-plan branches of get_limits).
    """
    limits = get_limits(user)
    raw = limits.get("projects", 1)
    raw = int(raw) if isinstance(raw, int | float) else 1
    if not settings.PAYWALL_ENABLED:
        return raw
    sub = getattr(user, "subscription", None) if user is not None else None
    if is_trialing_subscription(sub):
        free_cap = int(PLAN_LIMITS[Plan.FREE].get("projects", 1) or 1)
        return free_cap if raw == -1 else min(raw, free_cap)
    return raw

