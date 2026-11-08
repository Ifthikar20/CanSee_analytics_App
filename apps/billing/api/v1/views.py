"""
Billing API views (Polar-backed).

Plan, subscription and usage reads come from the local Subscription row and
the AI usage ledger; checkout, plan changes and the billing portal go
through Polar. Invoices are shown in Polar's billing portal (PortalView).
"""

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Subscription
from apps.billing.services.plan_service import SELF_SERVE_PLANS, PlanService

logger = logging.getLogger("billing")

MONTHLY_ONLY = "Billing is monthly only."


def _refused(code: str, message: str, http_status=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response(
        {"success": False, "error": {"code": code, "message": message}}, status=http_status,
    )


def _plan_error(plan) -> Response | None:
    """400 ``invalid_plan`` unless ``plan`` is one of the self-serve plans."""
    from apps.billing.services.polar_billing import INVALID_PLAN

    if plan not in SELF_SERVE_PLANS:
        return _refused("invalid_plan", INVALID_PLAN)
    return None


def _safe_origin(request) -> str:
    """Origin used to build checkout/portal return URLs.

    The Origin/Referer headers are attacker-controllable, and Polar's
    success redirect is NOT signed — trusting them blindly would let a
    crafted request bounce a paying customer to a foreign site after
    checkout. Only origins on the CORS allowlist (plus FRONTEND_URL)
    are honored; anything else falls back to FRONTEND_URL.
    """
    from urllib.parse import urlsplit

    from django.conf import settings as dj_settings

    allowed = set(getattr(dj_settings, "CORS_ALLOWED_ORIGINS", []) or [])
    canonical = getattr(dj_settings, "FRONTEND_URL", "").rstrip("/")
    if canonical:
        allowed.add(canonical)

    raw = request.META.get("HTTP_ORIGIN") or request.META.get("HTTP_REFERER") or ""
    parts = urlsplit(raw)
    candidate = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    if candidate and candidate in allowed:
        return candidate
    return canonical or "https://app.cansee.ai"


def _client_ip(request) -> str:
    """Customer IP for Polar's location-based tax/currency detection.

    Proxy-aware: the LEFT-most X-Forwarded-For hop is client-controlled
    (a customer could claim a low-tax jurisdiction's IP), so delegate to
    the hardened resolver that only trusts our own proxy hops.
    """
    from core.utils.ua_parser import get_client_ip

    return get_client_ip(request)


class BillingOverviewView(APIView):
    """Current subscription, plan details, and usage."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.billing.services import polar_billing
        from apps.billing.services.plan_limits import (
            limits_block,
            plan_for_subscription,
            subscription_state,
        )
        from core.utils.constants import SubscriptionStatus

        try:
            subscription = request.user.subscription
        except Subscription.DoesNotExist:
            subscription = None

        # Stale-state safety net: a paid checkout whose confirm redirect
        # never landed leaves the local row missing while Polar has the
        # subscription — and this page would tell a paying customer they
        # are on the Free plan. When there is neither an access-granting
        # row nor any Polar-managed row, ask Polar before answering.
        # (Rows that already carry a polar_subscription_id are owned by
        # the webhook/confirm sync — do not second-guess them here.)
        if polar_billing.is_configured() and not (
            subscription
            and (
                subscription.polar_subscription_id
                or subscription.status in (
                    SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING,
                )
            )
        ):
            try:
                # Cooldown-limited: at most one Polar round-trip per
                # user per window, so a polling client cannot trip the
                # shared circuit breaker.
                subscription = polar_billing.self_heal_from_polar(request.user)
            except Exception:
                logger.warning(
                    "Billing overview Polar state sync failed for %s",
                    request.user.id, exc_info=True,
                )

        # The mirror image of the guard above: a Polar-managed TRIALING
        # row whose trial end has passed is re-verified (cooldown-
        # limited, never raises) so a converted trial reads Active and a
        # failed charge reads Past due instead of "trialing" forever.
        subscription = polar_billing.reverify_ended_trial(request.user, subscription)
        state = subscription_state(subscription)

        # The plan the account is ACTUALLY on: an active/trialing
        # subscription grants its tier; everything else is Free. Derived
        # from the row we hold (NOT current_plan_for: the user instance
        # may cache a pre-sync "no subscription" miss), and never from
        # the denormalized user.plan, which defaults to a paid tier and
        # used to leak paid allowances to unsubscribed accounts.
        active_plan = str(plan_for_subscription(subscription))

        # Org members are entitled through the org's provisioned plan, not
        # their (usually absent) personal subscription. `source` tells the
        # billing page to hide personal checkout/portal UI entirely.
        source = "user"
        from core.permissions.org import org_features_enabled

        membership = None
        if org_features_enabled():
            membership = (
                request.user.org_memberships.select_related("organization")
                .order_by("created_at")
                .first()
            )
        org_block = None
        if membership:
            from apps.billing.services.plan_limits import _LEGACY_MAP

            org = membership.organization
            active_plan = str(_LEGACY_MAP.get(org.plan, org.plan))
            source = "organization"
            org_block = {"id": str(org.id), "name": org.name, "role": membership.role}

        plan_data = PlanService.get_plan(active_plan)

        # Determine segment
        segment = getattr(request.user, "segment", None) or "individual"

        return Response({
            "plan": active_plan,
            "source": source,
            "org": org_block,
            "segment": segment,
            "plan_details": plan_data,
            # Resolved allowances (trial-aware: a live trial keeps the Free
            # project cap until the first charge). -1 = unlimited. Same
            # shape as the session payload's `limits` block.
            "limits": limits_block(request.user),
            # False until the Polar token and both plan product ids are
            # configured — the frontend disables checkout and says so
            # instead of letting the button fail silently.
            "billing_ready": polar_billing.checkout_ready(),
            "subscription_status": subscription.status if subscription else "none",
            # Shared trial/tier vocabulary (same builder as the session
            # payload) so labels cannot drift between surfaces.
            "tier": state["tier"],
            "is_trialing": state["is_trialing"],
            "trial_end": state["trial_end"],
            # "month" | "year" | None — the cadence the subscription is
            # actually on (mirrored from Polar; period length can't tell
            # while trialing).
            "interval": state["interval"],
            # Raw plan on the subscription row (legacy values included).
            # The frontend needs it for non-access-granting states the
            # top-level `plan` reports as free — e.g. a past_due paying
            # customer must still be shown their paid plan, not "Free".
            "subscription_plan": subscription.plan if subscription else None,
            "current_period_end": (
                subscription.current_period_end.isoformat()
                if subscription and subscription.current_period_end else None
            ),
            "cancel_at_period_end": subscription.cancel_at_period_end if subscription else False,
            # True when this subscription is managed by the billing
            # provider (Polar) — gates the "Manage billing" portal button.
            "managed": bool(subscription.polar_subscription_id) if subscription else False,
            "provider": "polar" if subscription and subscription.polar_subscription_id else None,
        })


class PlansView(APIView):
    """List the plans: Free, 100 prompts, 200 prompts, Business.

    Public (the signed-out /pricing page reads it): the list is the same for
    everyone, so no credentials are read.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(PlanService.get_all_plans())


class CheckoutView(APIView):
    """Create a Polar checkout session for a self-serve plan.

    POST /api/v1/billing/checkout/  body: {"plan": "prompts_100" | "prompts_200"}.
    Subscribing always goes through Polar's hosted checkout; the Polar
    sandbox accepts test cards, so dev flows use the real checkout too.
    Business requires a sales contact; Pro is no longer offered.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        plan = data.get("plan")
        refused = _plan_error(plan)
        if refused is not None:
            return refused
        if data.get("annual"):
            return _refused("invalid_plan", MONTHLY_ONLY)

        origin = _safe_origin(request)

        from apps.billing.services import polar_billing
        from core.exceptions import CanseeException

        try:
            url = polar_billing.create_checkout(
                request.user, plan=plan, origin=origin,
                customer_ip=_client_ip(request),
            )
            return Response({"success": True, "data": {"checkout_url": url}})
        except CanseeException as e:
            # Keep the domain error code (e.g. already_subscribed) — the
            # frontend branches on it to recover instead of just
            # showing a failure toast.
            logger.warning(f"Checkout refused: {e}")
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": getattr(e, "code", "checkout_failed"),
                        "message": str(e),
                    },
                },
                status=getattr(e, "status_code", status.HTTP_400_BAD_REQUEST),
            )
        except Exception as e:
            # Unexpected failure: log the detail, return only a generic
            # message. Never echo raw exception text to the client — it
            # can carry upstream/internal detail.
            logger.error(f"Checkout creation failed: {e}")
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "checkout_failed",
                        "message": "We couldn't start the checkout. Please try again.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


class CheckoutConfirmView(APIView):
    """Confirm a completed Polar checkout after the success redirect.

    The frontend lands on /dashboard?checkout=success&checkout_id=... and
    posts the id here; we verify it against Polar and sync the
    subscription. This makes activation work in local dev where Polar
    cannot deliver webhooks; in production the webhook does the same
    thing first and this call becomes a no-op re-sync.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        checkout_id = str(request.data.get("checkout_id", "")).strip()
        if not checkout_id:
            return Response(
                {"success": False, "error": {"code": "missing_checkout_id",
                 "message": "checkout_id is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from apps.billing.services import polar_billing

        try:
            result = polar_billing.confirm_checkout(request.user, checkout_id)
            return Response({"success": True, "data": result})
        except Exception as e:
            logger.error("Checkout confirmation failed: %s", e)
            return Response(
                {"success": False, "error": {"code": "confirm_failed",
                 "message": "We couldn't verify the checkout. Refresh in a moment."}},
                status=status.HTTP_400_BAD_REQUEST,
            )


class PortalView(APIView):
    """Create a pre-authenticated Polar customer portal session."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return_url = f"{_safe_origin(request)}/billing"

        from apps.billing.services import polar_billing

        try:
            url = polar_billing.portal_url(request.user, return_url=return_url)
            return Response({"success": True, "data": {"portal_url": url}})
        except Exception as e:
            logger.error(f"Portal creation failed: {e}")
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "portal_failed",
                        "message": "We couldn't open the billing portal. Please try again.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


class SubscriptionCancelView(APIView):
    """
    Cancel the current user's subscription at the end of the period.

    Polar-managed subscriptions are updated via the Polar API and then
    re-synced; legacy local-only rows just flip the flag.

    POST /api/v1/billing/cancel/   -> sets cancel_at_period_end = True
    POST /api/v1/billing/resume/   -> sets cancel_at_period_end = False
    """
    permission_classes = [IsAuthenticated]
    cancel = True   # subclass flips to False for resume

    def post(self, request):
        try:
            sub = request.user.subscription
        except Subscription.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "no_subscription",
                 "message": "You don't have an active subscription."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if sub.polar_subscription_id:
            from apps.billing.services import polar_billing

            try:
                sub = polar_billing.set_cancel_at_period_end(
                    request.user, cancel=self.cancel,
                )
            except Exception as e:
                logger.error("Polar cancel/resume failed: %s", e)
                return Response(
                    {"success": False, "error": {"code": "billing_failed",
                     "message": "We couldn't update the subscription. Please try again."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            sub.cancel_at_period_end = self.cancel
            sub.save(update_fields=["cancel_at_period_end", "updated_at"])

        return Response({
            "success": True,
            "data": {
                "cancel_at_period_end": sub.cancel_at_period_end,
                "current_period_end": (
                    sub.current_period_end.isoformat()
                    if sub.current_period_end else None
                ),
            },
        })


class SubscriptionResumeView(SubscriptionCancelView):
    cancel = False


class UpgradeNowView(APIView):
    """End the free trial immediately and start the paid Pro plan.

    POST /api/v1/billing/upgrade-now/  body: {"annual": bool} (optional —
    omitted keeps the cadence the trial was started on). Returns the
    refreshed subscription state plus the resolved limits so the UI can
    unlock (e.g. the 5-project allowance) without another round-trip.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.billing.services import polar_billing
        from apps.billing.services.plan_limits import limits_block, subscription_state
        from core.exceptions import CanseeException

        annual = request.data.get("annual", None)
        if annual is not None:
            annual = bool(annual)
        try:
            sub = polar_billing.upgrade_trial_now(request.user, annual=annual)
        except CanseeException:
            raise
        except Exception as e:
            logger.error("Trial upgrade failed: %s", e)
            return Response(
                {"success": False, "error": {"code": "billing_failed",
                 "message": "We couldn't start your plan. Please try again."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The service re-synced the row; drop this instance's cached
        # relation so the limits below see the post-upgrade state.
        request.user.refresh_from_db()
        return Response({
            "success": True,
            "data": {
                "subscription": subscription_state(sub),
                "limits": limits_block(request.user),
            },
        })


class ChangePlanView(APIView):
    """Move an active subscription to the other self-serve plan.

    POST /api/v1/billing/change-plan/  body: {"plan": "prompts_100" | "prompts_200"}.
    The plan changes now: Polar charges the difference when the price goes
    up and credits the unused part when it goes down (a Pro row still in
    its trial switches free). Answers the subscription and the limits.

    The legacy body {"annual": bool} keeps switching a Pro row between
    monthly and annual billing; the self-serve plans are monthly only.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.billing.services import polar_billing
        from apps.billing.services.plan_limits import (
            limits_block,
            subscription_state,
            tier_for_subscription,
        )
        from core.exceptions import CanseeException
        from core.utils.constants import Plan

        data = request.data if isinstance(request.data, dict) else {}
        if "plan" in data:
            refused = _plan_error(data.get("plan"))
            if refused is not None:
                return refused
        elif "annual" in data:
            current = Subscription.objects.filter(user=request.user).first()
            if current is not None and tier_for_subscription(current) != Plan.PRO:
                return _refused("invalid_plan", MONTHLY_ONLY)
        else:
            from apps.billing.services.polar_billing import INVALID_PLAN

            return _refused("invalid_plan", INVALID_PLAN)
        try:
            if "plan" in data:
                sub = polar_billing.change_plan(request.user, plan=data["plan"])
            else:
                sub = polar_billing.change_billing_cycle(
                    request.user, annual=bool(data.get("annual")),
                )
        except CanseeException:
            raise
        except Exception as e:
            logger.error("Plan change failed: %s", e)
            return _refused("billing_failed", "We couldn't change your plan. Please try again.")
        # The service re-synced the row; drop this instance's cached
        # relation so the limits below see the new plan.
        request.user.refresh_from_db()
        return Response({
            "success": True,
            "data": {
                "subscription": subscription_state(sub),
                "limits": limits_block(request.user),
            },
        })


class AITokenUsageView(APIView):
    """Aggregate AI token spend for the current user's billing period.

    Same service (apps.metering.services.usage_reader) as the Settings
    page's AIUsageView, so the two surfaces agree by construction —
    previously each ran its own aggregation over a different window.
    Keeps this endpoint's historical envelope keys (cost_usd, call_count)
    for the Billing page card.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.metering.services.usage_reader import get_period_usage

        usage = get_period_usage(request.user)

        def _shape(rows, key):
            return [
                {
                    key: r.get(key) or "",
                    "tokens": int(r.get("tokens") or 0),
                    "cost_usd": float(r.get("cost") or 0),
                }
                for r in rows
            ]

        payload = {
            "period": usage["period"],
            "source": usage["source"],
            "call_count": usage["totals"]["calls"],
            "totals": {
                "input_tokens": usage["totals"]["input_tokens"],
                "output_tokens": usage["totals"]["output_tokens"],
                "total_tokens": usage["totals"]["total_tokens"],
                "cost_usd": usage["totals"]["estimated_cost_usd"],
            },
            "by_provider": _shape(usage["by_provider"], "provider"),
            "by_module": _shape(usage["by_module"], "module"),
            "allowance": usage["allowance"],
            "prompt_runs": usage.get("prompt_runs"),
        }
        # Bare payload: the global EnvelopeRenderer wraps it once as
        # {success, data}. The old explicit {"success", "data"} return was
        # double-wrapped by that renderer, which is why the Billing card
        # read one envelope level too shallow.
        return Response(payload)


class PromptRunsView(APIView):
    """GET: the account's prompt runs this billing period.

    One prompt run is one prompt answered by one AI engine. The free plan
    gets FREE_MONTHLY_PROMPT_RUNS a period, shared by every project and
    engine of the account; paid plans are unlimited. ``?website_id`` reads
    the account of that project's owner (a project shared with you counts
    on its owner's runs). Body: prompt_runs.status.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.metering.services import prompt_runs

        website = None
        website_id = request.query_params.get("website_id")
        if website_id:
            from apps.websites.services.website_service import WebsiteService

            website = WebsiteService.get_for_user(
                user=request.user, website_id=website_id, min_role="viewer",
            )
        return Response(prompt_runs.status(request.user, website))


class PaywallDismissView(APIView):
    """POST — record that the user chose to continue on the Free plan.

    Idempotent: repeat calls keep the original dismissal timestamp.
    Paying users are a no-op (the paywall never routes for them) and the
    flag stays unset, so a later lapse re-surfaces the paywall once.
    Deliberately settings-independent: inert but harmless while
    PAYWALL_ENABLED is off.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone

        from apps.billing.services.plan_limits import is_paying
        from core.logging.audit_logger import audit_log

        user = request.user

        if is_paying(user):
            return Response({
                "paywall_dismissed": user.paywall_dismissed_at is not None,
                "dismissed_at": (
                    user.paywall_dismissed_at.isoformat()
                    if user.paywall_dismissed_at else None
                ),
                "already_paying": True,
            })

        if user.paywall_dismissed_at is None:
            user.paywall_dismissed_at = timezone.now()
            user.save(update_fields=["paywall_dismissed_at", "updated_at"])
            audit_log("billing.paywall_dismissed", user=user, request=request)

        return Response({
            "paywall_dismissed": True,
            "dismissed_at": user.paywall_dismissed_at.isoformat(),
            "already_paying": False,
        })
