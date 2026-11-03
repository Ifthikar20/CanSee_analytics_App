from django.db import models


class Segment(models.TextChoices):
    INDIVIDUAL = "individual", "Individual"
    ENTERPRISE = "enterprise", "Enterprise"


class Plan(models.TextChoices):
    # ── Live model (2026-08): Free -> Pro $45/mo self-serve -> Business custom ──
    FREE = "free", "Free"
    PRO = "pro", "Pro ($45/mo)"
    BUSINESS = "business", "Business (custom)"
    # Legacy aliases for migration compatibility — these still appear
    # on existing Subscription/User rows and must keep resolving to a
    # real plan via PLAN_LIMITS until a backfill migrates them.
    INDIVIDUAL = "individual", "Individual (Legacy)"
    ENTERPRISE = "enterprise", "Enterprise (Legacy)"
    STARTER = "starter", "Starter (Legacy)"
    GROWTH = "growth", "Growth (Legacy)"
    SCALE = "scale", "Scale (Legacy)"


# ── Feature limits per plan ──────────────────────────────────────────
#
# ``max_prompts_per_audit`` is the headline LLM-ranking gate — every
# audit reads it off the user's current plan (max_prompts_for_user) and
# caps the prompt list accordingly. Bumping a user's tier widens the cap
# on the next audit immediately.
PLAN_LIMITS = {
    # Free: what an account WITHOUT an active/trialing subscription gets.
    # The AI allowance is a small acquisition cost (settings.
    # AI_FREE_MONTHLY_CAP_USD, default $1/month) — enough to feel the
    # product, not enough to burn margin.
    Plan.FREE: {
        "segment": Segment.INDIVIDUAL,
        "price_monthly": 0,
        "price_yearly": 0,
        "trial_days": 0,
        "projects": 1,
        "pageviews": 10_000,
        "team_members": 1,
        "ai_credits_monthly": 50,
        "integrations": 1,
        "competitors": 3,
        "max_prompts_per_audit": 5,
        "max_audits_per_month": 2,
        # Dedicated prompt allowance per account per calendar month —
        # every prompt queued into an audit draws it down. -1 = unlimited.
        "monthly_prompts": 10,
    },
    # Pro carries the full self-serve feature set at $45/mo. The AI spend
    # wall (65% of price -> $29.25/mo of model cost) is what protects
    # margin, so the feature limits can stay generous.
    Plan.PRO: {
        "segment": Segment.INDIVIDUAL,
        "price_monthly": 45,
        "price_yearly": 450,
        "trial_days": 7,
        "projects": 5,
        "pageviews": 250_000,
        "team_members": 5,
        "ai_credits_monthly": 600,
        "integrations": 10,
        "competitors": 25,
        "max_prompts_per_audit": 15,
        "max_audits_per_month": 30,           # daily cadence cap
        "monthly_prompts": 100,
    },
    Plan.BUSINESS: {
        "segment": Segment.ENTERPRISE,
        "price_monthly": -1,  # custom — contact sales
        "price_yearly": -1,
        "trial_days": 0,
        "projects": -1,
        "pageviews": -1,
        "team_members": -1,
        "ai_credits_monthly": -1,
        "integrations": -1,
        "competitors": -1,
        "max_prompts_per_audit": 50,
        "max_audits_per_month": -1,
        # Per-SEAT default for enterprise orgs; each org can carry a
        # negotiated override (Organization.monthly_prompt_allowance).
        "monthly_prompts": 200,
    },
}

# Legacy aliases — keep resolving so existing Subscription/User rows
# don't 500 on read. Every historical self-serve tier maps to Pro; the
# historical top tiers map to Business.
PLAN_LIMITS[Plan.INDIVIDUAL] = PLAN_LIMITS[Plan.PRO]
PLAN_LIMITS[Plan.STARTER] = PLAN_LIMITS[Plan.PRO]
PLAN_LIMITS[Plan.GROWTH] = PLAN_LIMITS[Plan.PRO]
PLAN_LIMITS[Plan.ENTERPRISE] = PLAN_LIMITS[Plan.BUSINESS]
PLAN_LIMITS[Plan.SCALE] = PLAN_LIMITS[Plan.BUSINESS]


# Fallback for plan values that are not PLAN_LIMITS keys themselves
# (e.g. "team"); see legacy_plan_key().
LEGACY_TIER_KEY = {
    "pro": "growth",
    "business": "scale",
    "individual": "growth",
    "enterprise": "scale",
    "free": "starter",
    "team": "scale",
    "starter": "starter",
    "growth": "growth",
    "scale": "scale",
}


def max_prompts_for_user(user) -> int:
    """
    Return the prompts-per-audit cap for ``user``'s current plan.

    Org members resolve through their organization: an explicit
    ``max_prompts_per_audit`` on the org (a negotiated enterprise
    package) always wins, otherwise the org plan's number applies.
    Everyone else resolves through ``current_plan_for`` (the single
    source of truth): an active/trialing subscription grants its tier,
    everything else — no row, canceled, past due, lapsed trial — is the
    Free cap. Never returns 0: even a Free user can run a tiny 5-prompt
    audit so the "first run" experience isn't gated.
    """
    from django.conf import settings

    org = _org_for(user)
    if org is not None and org.max_prompts_per_audit:
        # Custom contract: binds regardless of the paywall dev switch.
        return int(org.max_prompts_per_audit)

    if org is not None:
        limits = PLAN_LIMITS.get(legacy_plan_key(org.plan)) or PLAN_LIMITS[Plan.BUSINESS]
    elif not settings.PAYWALL_ENABLED:
        limits = PLAN_LIMITS[Plan.BUSINESS]
    else:
        from apps.billing.services.plan_limits import current_plan_for

        limits = PLAN_LIMITS.get(current_plan_for(user)) or PLAN_LIMITS[Plan.FREE]
    raw = limits.get("max_prompts_per_audit") or 5
    cap = int(raw) if isinstance(raw, int | float) else 5
    return cap if cap > 0 else 50  # treat -1 as "effectively unlimited"


def legacy_plan_key(plan: str) -> str:
    """Map any plan value (incl. legacy aliases) to a live PLAN_LIMITS key."""
    return plan if plan in PLAN_LIMITS else LEGACY_TIER_KEY.get(plan, "pro")


def _org_for(user):
    """The user's organization, or None. Cached on the user instance.

    Deliberately NOT the ``_org_cache`` attribute: User.effective_plan
    assumes that one is only ever set to a real Organization, so caching
    a miss there would crash it.
    """
    if user is None or not getattr(user, "pk", None):
        return None
    if hasattr(user, "_org_or_none_cache"):
        return user._org_or_none_cache
    if not hasattr(user, "org_memberships"):
        return None
    membership = (
        user.org_memberships.select_related("organization")
        .order_by("created_at")
        .first()
    )
    user._org_or_none_cache = membership.organization if membership else None
    return user._org_or_none_cache


class UserRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    EDITOR = "editor", "Editor"
    VIEWER = "viewer", "Viewer"


class OrgRole(models.TextChoices):
    """Canonical organization role vocabulary.

    ``member`` is the org-level read-write role (what WebsiteMembership's
    legacy vocabulary called "editor" — ROLE_HIERARCHY ranks them equal).
    ``viewer`` is read-only. Owner is unique per org and only transferable,
    never grantable through the API.
    """

    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


# Public mailbox providers that can never be claimed as an organization
# domain: possession of an @gmail.com address proves nothing about a
# company, and letting one through would hand domain auto-join to the
# entire provider's user base. Enforced in OrgDomain.clean().
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com",
    "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me",
    "aol.com", "mail.com", "gmx.com", "gmx.net",
    "zoho.com", "fastmail.com", "hey.com",
    "mail.ru", "yandex.com", "yandex.ru", "qq.com", "naver.com", "163.com",
})


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past Due"
    CANCELED = "canceled", "Canceled"
    TRIALING = "trialing", "Trialing"
    INCOMPLETE = "incomplete", "Incomplete"
