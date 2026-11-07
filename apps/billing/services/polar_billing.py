"""
Polar-backed billing: product catalog, checkout, plan sync, portal.

Replaces the Stripe checkout path. The flow:

  1. The owner creates the two plan products in Polar (100 prompts $80 and
     200 prompts $140, monthly, no trial) and their ids go into env
     (POLAR_PRODUCT_PROMPTS_100_ID, POLAR_PRODUCT_PROMPTS_200_ID). The Pro
     products (`polar_billing_bootstrap`) stay for Pro subscribers.
  2. CheckoutView -> create_checkout() -> redirect the user to Polar's
     hosted checkout (sandbox accepts Stripe test cards).
  3. The user returns to /dashboard?checkout=success&checkout_id=... and
     the frontend calls CheckoutConfirmView -> confirm_checkout(), which
     pulls the checkout + customer state from Polar and updates the local
     Subscription row and user.plan. (In production the webhook does the
     same server-side; the redirect confirmation makes local dev work
     without a public webhook URL.)
  4. sync_from_customer_state() is the single writer for subscription
     state: webhook, confirm, and manual refresh all converge here.

Populating Subscription.current_period_start/end here is what flips
every usage window (metering Pass 1's billing_period_for seam) from
calendar months to real billing cycles — by design, no other code
changes needed.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.billing.models import Subscription
from apps.billing.services.plan_limits import tier_for_subscription, trial_ended
from apps.metering import polar_client
from core.exceptions import CanseeException
from core.logging.audit_logger import audit_log
from core.utils.constants import (
    PLAN_LIMITS,
    PLAN_NAMES,
    Plan,
    SubscriptionStatus,
    legacy_plan_key,
)

logger = logging.getLogger("billing")

PRODUCT_PRO_MONTHLY = "Cansee Pro (Monthly)"
PRODUCT_PRO_ANNUAL = "Cansee Pro (Annual)"

# The self-serve plans and the setting holding each one's Polar product id.
# No product id is written in code.
PLAN_PRODUCT_SETTINGS = {
    Plan.PROMPTS_100: "POLAR_PRODUCT_PROMPTS_100_ID",
    Plan.PROMPTS_200: "POLAR_PRODUCT_PROMPTS_200_ID",
}
CHECKOUT_NOT_READY = "Checkout isn't set up yet."
INVALID_PLAN = "Choose the 100 or 200 prompt plan."

# Polar subscription status -> local SubscriptionStatus. Anything that
# still grants access maps to ACTIVE/TRIALING; terminal states to
# CANCELED so entitlement gates close. Customer state only ever reports
# active/trialing subscriptions; the non-access entries are reached
# through the by-id lookup in sync_from_customer_state (a failed
# trial-end charge lands as PAST_DUE, not as a silent CANCELED).
_STATUS_MAP = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.CANCELED,
    "canceled": SubscriptionStatus.CANCELED,
    "revoked": SubscriptionStatus.CANCELED,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.CANCELED,
}


def product_id_for(plan) -> str:
    """The Polar product id of a self-serve plan ("" when unset or not one)."""
    setting = PLAN_PRODUCT_SETTINGS.get(plan)
    return str(getattr(settings, setting, "") or "") if setting else ""


def _pro_product_ids() -> set[str]:
    return {
        pid for pid in (
            settings.POLAR_PRODUCT_PRO_MONTHLY_ID,
            settings.POLAR_PRODUCT_PRO_ANNUAL_ID,
        ) if pid
    }


def is_configured() -> bool:
    """Polar can be reached and knows at least one of our products: enough
    to sync, confirm, open the portal, cancel and resume, upgrade a trial
    and take webhooks, so deploying before the plan products exist never
    strands a paying Pro customer."""
    return bool(
        polar_client.is_configured()
        and (_pro_product_ids() or any(product_id_for(p) for p in PLAN_PRODUCT_SETTINGS))
    )


def checkout_ready() -> bool:
    """Checkout and plan changes can run: the token and both plan products."""
    return bool(
        polar_client.is_configured()
        and all(product_id_for(p) for p in PLAN_PRODUCT_SETTINGS)
    )


def _require_configured() -> None:
    if not is_configured():
        raise CanseeException(
            "Billing is not configured yet. Please try again later.",
            code="billing_not_configured",
            status_code=503,
        )


def _require_checkout_ready() -> None:
    if not checkout_ready():
        raise CanseeException(
            CHECKOUT_NOT_READY, code="billing_not_configured", status_code=503,
        )


def _require_self_serve(plan) -> None:
    if not isinstance(plan, str) or plan not in PLAN_PRODUCT_SETTINGS:
        raise CanseeException(INVALID_PLAN, code="invalid_plan", status_code=400)


def _require_pro_product(annual: bool) -> str:
    """The Pro product of one cadence, for the Pro-only paths: is_configured()
    no longer implies the Pro products are set."""
    product_id = _product_id_for(annual)
    if not product_id:
        raise CanseeException(
            "Billing is not configured yet. Please try again later.",
            code="billing_not_configured",
            status_code=503,
        )
    return product_id


def _product_id_for(annual: bool) -> str:
    """The Pro product of one cadence (Pro subscribers only)."""
    return (
        settings.POLAR_PRODUCT_PRO_ANNUAL_ID
        if annual
        else settings.POLAR_PRODUCT_PRO_MONTHLY_ID
    )


def _plan_for_product(product_id: str) -> str | None:
    """The plan a Polar product grants. Never matches an unset setting."""
    if not product_id:
        return None
    for plan in PLAN_PRODUCT_SETTINGS:
        if product_id == product_id_for(plan):
            return plan
    if product_id in _pro_product_ids():
        return Plan.PRO
    return None


# ── Catalog ───────────────────────────────────────────────────────────────


def bootstrap_products() -> dict[str, str]:
    """Find-or-create the Pro products; returns {name: product_id}.

    Idempotent by product name. Prices are USD cents; trial is 7 days on
    both cadences (the product-level default the checkout inherits).
    """
    wanted = {
        PRODUCT_PRO_MONTHLY: {
            "name": PRODUCT_PRO_MONTHLY,
            "description": "Cansee Pro subscription, billed monthly.",
            "recurring_interval": "month",
            "prices": [{"amount_type": "fixed", "price_amount": 45_00}],
            "trial_interval": "day",
            "trial_interval_count": 7,
        },
        PRODUCT_PRO_ANNUAL: {
            "name": PRODUCT_PRO_ANNUAL,
            "description": "Cansee Pro subscription, billed yearly (2 months free).",
            "recurring_interval": "year",
            "prices": [{"amount_type": "fixed", "price_amount": 450_00}],
            "trial_interval": "day",
            "trial_interval_count": 7,
        },
    }
    existing = {p.name: str(p.id) for p in polar_client.list_products()}
    out = {}
    for name, request in wanted.items():
        if name in existing:
            out[name] = existing[name]
            continue
        created = polar_client.create_product(request)
        out[name] = str(created.id)
    return out


# ── Checkout ──────────────────────────────────────────────────────────────

# Minimum interval between request-path Polar state syncs per user. The
# overview and pre-checkout self-heals run on hot authenticated paths;
# without a floor, one account polling them could drive unbounded Polar
# API calls and trip the SHARED circuit breaker for every user.
_SELF_HEAL_COOLDOWN_S = 60


def self_heal_from_polar(user) -> Subscription | None:
    """Rate-limited wrapper around sync_from_customer_state for
    request-path self-heals. cache.add is atomic: at most one Polar
    round-trip per user per cooldown window; every other caller in the
    window just reads the local row."""
    key = f"billing:selfheal:{user.id}"
    if not cache.add(key, 1, _SELF_HEAL_COOLDOWN_S):
        return Subscription.objects.filter(user=user).first()
    return sync_from_customer_state(user)


def reverify_ended_trial(user, sub):
    """Settle a TRIALING row whose trial end has passed.

    At trial end Polar charges the card and flips the subscription to
    active; the webhook mirrors that here — except when no webhook can
    arrive (dev: localhost) or one was missed. Without this, such a row
    would read "trialing" forever. Re-sync through the cooldown-limited
    self-heal so a converted trial lands ACTIVE with its paid period and
    a failed charge lands PAST_DUE (by-id lookup). Request-path only:
    never raises, returns the row to use.
    """
    if not (
        sub is not None
        and sub.polar_subscription_id
        and trial_ended(sub)
        and is_configured()
    ):
        return sub
    try:
        return self_heal_from_polar(user) or sub
    except Exception:
        logger.warning("Ended-trial re-verify failed for %s", user.id, exc_info=True)
        return sub


def create_checkout(user, *, plan: str, origin: str, customer_ip: str = "") -> str:
    """Create a Polar checkout for a self-serve plan (100 or 200 prompts,
    monthly); returns the hosted checkout URL."""
    _require_self_serve(plan)
    _require_checkout_ready()

    sub = Subscription.objects.filter(user=user).first()
    if not (
        sub
        and (
            sub.polar_subscription_id
            or sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
        )
    ):
        # The local row says unpaid, but Polar may know better: a paid
        # checkout whose confirm redirect never landed (closed tab,
        # broken success_url) leaves the local DB stale, the paywall
        # gate looping, and Polar refusing the duplicate checkout with
        # "You already have an active subscription" — a trap with no
        # exit. Self-heal from Polar's customer state before creating
        # anything. Rows already carrying a polar_subscription_id are
        # owned by the webhook/confirm sync and are not second-guessed
        # (an ex-subscriber goes straight to a fresh checkout).
        # Best-effort: a sync failure must not block a legitimate first
        # checkout.
        try:
            sub = self_heal_from_polar(user)
        except Exception:
            logger.warning(
                "Pre-checkout Polar state sync failed for %s; continuing",
                user.id, exc_info=True,
            )
    # A trial that ended upstream (charge failed, canceled) must not trap
    # the user behind a stale local TRIALING row: settle it first.
    sub = reverify_ended_trial(user, sub)
    if sub and sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        raise CanseeException(
            "You already have an active subscription. Use Manage billing "
            "to change your plan.",
            code="already_subscribed",
            status_code=400,
        )

    # {CHECKOUT_ID} is substituted by Polar on redirect; the billing page
    # posts it to /billing/confirm/ so activation works without waiting
    # for a webhook (which cannot reach localhost in dev). The redirect
    # itself is NOT signed by Polar, which is why confirmation always
    # re-verifies the checkout against the API before granting anything.
    success_url = f"{origin}/billing?checkout=success&checkout_id={{CHECKOUT_ID}}"
    common = {
        "product_ids": [product_id_for(plan)],
        "external_customer_id": str(user.id),
        "success_url": success_url,
        "metadata": {"plan": str(plan)},
        "customer_ip_address": customer_ip or None,
    }
    try:
        checkout = polar_client.create_checkout(customer_email=user.email, **common)
    except polar_client.PolarRejected as exc:
        # Polar validates that the email's domain can receive mail; dev
        # and test accounts (and the odd real user on a mail-less
        # domain) fail that check. Retry without prefilling — the hosted
        # checkout then asks the customer to type an email, and identity
        # stays bound through external_customer_id.
        if "customer_email" not in str(exc):
            raise
        # Log the user id, not the email — no PII in application logs.
        logger.warning(
            "Checkout email for user %s rejected by Polar; retrying without prefill",
            user.id,
        )
        checkout = polar_client.create_checkout(customer_email=None, **common)
    audit_log(
        "billing.checkout_created",
        user=user,
        metadata={"checkout_id": str(checkout.id), "plan": str(plan), "provider": "polar"},
    )
    url = str(checkout.url)
    # Match the app's light theme on the hosted page.
    return url + ("&" if "?" in url else "?") + "theme=light"


def confirm_checkout(user, checkout_id: str) -> dict:
    """Post-redirect confirmation: verify the checkout belongs to this
    user and succeeded, then sync subscription state from Polar."""
    _require_configured()
    checkout = polar_client.get_checkout(checkout_id)

    if str(getattr(checkout, "external_customer_id", "") or "") != str(user.id):
        raise CanseeException(
            "This checkout does not belong to your account.",
            code="checkout_mismatch",
            status_code=403,
        )

    status = str(getattr(checkout, "status", ""))
    if status not in ("succeeded", "confirmed"):
        return {"status": status, "active": False}

    sub = sync_from_customer_state(user)
    return {
        "status": status,
        "active": bool(
            sub
            and sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
        ),
        "plan": getattr(sub, "plan", None),
    }


# ── State sync (the single writer) ────────────────────────────────────────


def _apply_polar_subscription(sub, polar_sub, plan) -> None:
    """Copy a Polar subscription object onto the local row (no save)."""
    sub.polar_subscription_id = str(polar_sub.id)
    sub.plan = plan
    # SDK statuses are enums whose str() is "Status.TRIALING" —
    # unwrap .value before mapping or trials render as active.
    raw_status = getattr(polar_sub, "status", "active")
    raw_status = getattr(raw_status, "value", raw_status)
    sub.status = _STATUS_MAP.get(str(raw_status), SubscriptionStatus.ACTIVE)
    sub.current_period_start = getattr(polar_sub, "current_period_start", None)
    sub.current_period_end = getattr(polar_sub, "current_period_end", None)
    sub.cancel_at_period_end = bool(getattr(polar_sub, "cancel_at_period_end", False))
    # recurring_interval is an enum ("month"/"year") — unwrap like status.
    raw_interval = getattr(polar_sub, "recurring_interval", None)
    raw_interval = getattr(raw_interval, "value", raw_interval)
    if raw_interval:
        sub.interval = str(raw_interval)


def _lookup_managed_subscription(user, subscription_id):
    """Fetch the subscription we manage by id; ``(polar_sub, plan)`` or None.

    Customer state lists only active/trialing subscriptions, so a
    subscription that dropped out of it is indistinguishable there
    between "charge failed" (past_due), "dunning exhausted" (unpaid) and
    "canceled". The by-id read carries the real status. None means gone,
    not ours, or not one of our products — callers fall back to CANCELED.
    ``PolarUnavailable`` propagates on purpose: the local row is left
    untouched rather than guessed (webhook -> 500 -> Polar redelivers;
    request-path callers catch and log).
    """
    try:
        polar_sub = polar_client.get_subscription(subscription_id)
    except polar_client.PolarRejected:
        return None
    customer = getattr(polar_sub, "customer", None)
    ext = str(getattr(customer, "external_id", "") or "")
    if ext and ext != str(user.id):
        logger.warning(
            "Polar subscription %s is not owned by user %s", subscription_id, user.id
        )
        return None
    plan = _plan_for_product(str(getattr(polar_sub, "product_id", "")))
    if plan is None:
        return None
    return polar_sub, plan


def sync_from_customer_state(user) -> Subscription | None:
    """Pull the customer state from Polar and mirror it locally.

    Called from checkout confirmation, the webhook, and manual refresh.
    Picks the newest access-granting subscription; if none exists and the
    local row is Polar-managed, the managed subscription is looked up by
    id so its real non-access status (past_due, canceled, ...) is
    mirrored; if it is gone entirely the row is marked canceled (plan
    value is kept for display; entitlement gates read status).
    """
    try:
        state = polar_client.get_customer_state(str(user.id))
    except polar_client.PolarRejected:
        # No Polar customer yet — nothing to sync.
        return Subscription.objects.filter(user=user).first()

    subs = list(getattr(state, "active_subscriptions", None) or [])
    chosen = None
    for s in subs:
        plan = _plan_for_product(str(getattr(s, "product_id", "")))
        if plan is None:
            continue
        if chosen is None or (
            getattr(s, "started_at", None)
            and getattr(chosen[0], "started_at", None)
            and s.started_at > chosen[0].started_at
        ):
            chosen = (s, plan)

    # Nothing access-granting upstream but a managed row locally: ask for
    # that subscription by id BEFORE taking the row lock (network call).
    fallback = None
    if chosen is None:
        managed_id = (
            Subscription.objects.filter(user=user)
            .values_list("polar_subscription_id", flat=True)
            .first()
        )
        if managed_id:
            fallback = _lookup_managed_subscription(user, managed_id)

    with transaction.atomic():
        sub, created = Subscription.objects.select_for_update().get_or_create(user=user)
        if chosen is not None:
            polar_sub, plan = chosen
            _apply_polar_subscription(sub, polar_sub, plan)
            sub.save()
            if user.plan != plan:
                user.plan = plan
                user.save(update_fields=["plan"])
        elif fallback is not None:
            # Still exists upstream in a non-access state: mirror the REAL
            # status. past_due keeps the paid tier visible with a payment
            # notice; canceled/unpaid/revoked close the gate.
            _apply_polar_subscription(sub, *fallback)
            sub.save()
        elif sub.polar_subscription_id:
            # Previously Polar-managed, now gone upstream: access ends.
            sub.status = SubscriptionStatus.CANCELED
            sub.save(update_fields=["status", "updated_at"])
        elif created:
            # get_or_create minted this row a moment ago and Polar has
            # nothing for the user. The model default (TRIALING) would
            # leak paid-tier feature gates through current_plan_for, so
            # a row born from an empty sync must deny access.
            sub.status = SubscriptionStatus.CANCELED
            sub.save(update_fields=["status", "updated_at"])

    audit_log(
        "billing.synced_from_polar",
        user=user,
        metadata={"status": sub.status, "plan": sub.plan},
    )
    return sub


# ── Portal / lifecycle ────────────────────────────────────────────────────


def portal_url(user, *, return_url: str | None = None) -> str:
    _require_configured()
    return polar_client.create_portal_url(str(user.id), return_url=return_url)


def set_cancel_at_period_end(user, cancel: bool) -> Subscription:
    """Cancel at period end (or resume) via the Polar API, then re-sync."""
    _require_configured()
    sub = Subscription.objects.filter(user=user).first()
    if not sub or not sub.polar_subscription_id:
        raise CanseeException(
            "No active subscription to update.",
            code="no_subscription",
            status_code=400,
        )
    polar_client.update_subscription(
        sub.polar_subscription_id, {"cancel_at_period_end": cancel}
    )
    audit_log(
        "billing.cancel_at_period_end" if cancel else "billing.resumed",
        user=user,
        metadata={"subscription_id": sub.polar_subscription_id},
    )
    synced = sync_from_customer_state(user)
    return synced or sub


def upgrade_trial_now(user, *, annual: bool | None = None) -> Subscription:
    """End a live free trial immediately: Polar charges the first invoice
    now and the subscription converts to ACTIVE Pro.

    ``annual`` optionally switches the billing cadence in the same PATCH
    (None keeps whatever cadence the trial was started on). The follow-up
    sync mirrors whatever the charge produced — ACTIVE on success,
    PAST_DUE on a failed card — so entitlements never outrun payment.
    """
    from apps.billing.services.plan_limits import is_trialing_subscription

    _require_configured()
    sub = Subscription.objects.filter(user=user).first()
    if not sub or not sub.polar_subscription_id:
        raise CanseeException(
            "No subscription to upgrade.", code="no_subscription", status_code=400,
        )
    if not is_trialing_subscription(sub):
        raise CanseeException(
            "Your subscription isn't in a free trial.",
            code="not_trialing",
            status_code=400,
        )

    product_id = None
    if annual is not None:
        want = "year" if annual else "month"
        if sub.interval != want:
            product_id = _require_pro_product(annual)

    polar_client.update_subscription_plan(
        sub.polar_subscription_id, end_trial_now=True, product_id=product_id,
    )
    audit_log(
        "billing.trial_upgraded_now",
        user=user,
        metadata={
            "subscription_id": sub.polar_subscription_id,
            "annual": annual,
        },
    )
    synced = sync_from_customer_state(user)
    return synced or sub


def change_billing_cycle(user, *, annual: bool) -> Subscription:
    """Switch the Pro subscription between monthly and annual billing.

    On an ACTIVE subscription the price difference is settled immediately
    (proration behavior "invoice"); during a trial the product swap costs
    nothing and the new price simply applies when the trial converts.
    """
    _require_configured()
    sub = Subscription.objects.filter(user=user).first()
    if not sub or not sub.polar_subscription_id:
        raise CanseeException(
            "No subscription to update.", code="no_subscription", status_code=400,
        )
    if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        raise CanseeException(
            "Your subscription can't change plans right now.",
            code="not_active",
            status_code=400,
        )
    want = "year" if annual else "month"
    if sub.interval == want:
        raise CanseeException(
            "You're already on annual billing." if annual
            else "You're already on monthly billing.",
            code="already_on_plan",
            status_code=400,
        )

    polar_client.update_subscription_plan(
        sub.polar_subscription_id,
        product_id=_require_pro_product(annual),
        proration_behavior=(
            "invoice" if sub.status == SubscriptionStatus.ACTIVE else None
        ),
    )
    audit_log(
        "billing.cycle_changed",
        user=user,
        metadata={
            "subscription_id": sub.polar_subscription_id,
            "annual": annual,
        },
    )
    synced = sync_from_customer_state(user)
    return synced or sub


def _price_monthly(plan) -> int:
    raw = PLAN_LIMITS.get(legacy_plan_key(plan or ""), {}).get("price_monthly") or 0
    return raw if isinstance(raw, int | float) and raw > 0 else 0


def change_plan(user, *, plan: str) -> Subscription:
    """Move an active subscription (a self-serve plan, or Pro) to the
    product of ``plan``, now.

    Moving up invoices the difference at once (proration "invoice"); moving
    down credits the unused part to the next invoice ("prorate"). A Pro row
    still in its free trial swaps for free (no proration), as
    change_billing_cycle does: the new price applies when the trial converts.
    """
    _require_self_serve(plan)
    _require_checkout_ready()
    sub = Subscription.objects.filter(user=user).first()
    if not sub or not sub.polar_subscription_id:
        raise CanseeException(
            "No subscription to update.", code="no_subscription", status_code=400,
        )
    # The self-serve plans have no trial: a trialing row is a Pro trial.
    trialing_pro = (
        sub.status == SubscriptionStatus.TRIALING and tier_for_subscription(sub) == Plan.PRO
    )
    if sub.status != SubscriptionStatus.ACTIVE and not trialing_pro:
        raise CanseeException(
            "Your subscription can't change plans right now.",
            code="not_active",
            status_code=400,
        )
    if sub.plan == plan:
        raise CanseeException(
            f"You're already on {PLAN_NAMES[plan]}.",
            code="already_on_plan",
            status_code=400,
        )

    if trialing_pro:
        proration = None
    elif _price_monthly(plan) > _price_monthly(sub.plan):
        proration = "invoice"
    else:
        proration = "prorate"
    polar_client.update_subscription_plan(
        sub.polar_subscription_id,
        product_id=product_id_for(plan),
        proration_behavior=proration,
    )
    audit_log(
        "billing.plan_changed",
        user=user,
        metadata={
            "subscription_id": sub.polar_subscription_id,
            "from": sub.plan,
            "to": str(plan),
            "proration": proration,
        },
    )
    synced = sync_from_customer_state(user)
    return synced or sub


def touch_synced_at(sub: Subscription) -> None:
    sub.updated_at = timezone.now()
