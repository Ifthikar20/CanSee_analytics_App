"""Internal ops API — consumed ONLY by the admin servers (the ftb-min
admin dashboard and the Cansee console server), never by a browser.

Gated by the ``X-Admin-Key`` header against ``settings.ADMIN_OPS_KEY``.
An empty setting disables the surface entirely, and every failure mode
is a 404 — to anyone probing without the key these endpoints are
indistinguishable from routes that do not exist. The admin server holds
the key server-side and proxies for its UI; browsers never see it. In
production, additionally restrict this path prefix to the admin host at
the network layer (reverse proxy / firewall).

Read-only by design: the admin dashboard observes; it does not mutate
tenant data.

Token usage everywhere here comes from one source, the AITokenUsage
ledger, through apps.accounts.services.token_usage (see its docstring
for why the AgentRun and audit rollups are never added in).
"""

import ipaddress
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import RateLimited, ResourceNotFound
from core.logging.audit_logger import audit_log


class InternalAdminView(APIView):
    """Base gate for the internal ops surface. Three independent checks,
    all failing as 404 so the endpoints are unenumerable:

      1. Source IP inside ADMIN_OPS_ALLOWED_CIDRS ("only from that
         server"). REMOTE_ADDR is used — never a forwarded header, which
         any caller can forge. Empty list = the surface is OFF.
      2. Constant-time X-Admin-Key match. Empty key = OFF.
      3. Per-IP token-bucket rate limit.

    Every request is additionally audit-logged (path + source IP) on top
    of the middleware's request log. No JWT/session auth is involved.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    # In-process sliding window: fail-closed by construction (no cache /
    # Redis dependency that could fail open on an auth-adjacent control).
    # One admin server talks to one worker set; per-process is fine.
    _WINDOW_S = 60
    _MAX_PER_WINDOW = 120
    _requests: dict = defaultdict(deque)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)

        ip = request.META.get("REMOTE_ADDR", "") or ""
        cidrs = getattr(settings, "ADMIN_OPS_ALLOWED_CIDRS", []) or []
        try:
            addr = ipaddress.ip_address(ip)
            ip_ok = any(
                addr in ipaddress.ip_network(c, strict=False) for c in cidrs
            )
        except ValueError:
            ip_ok = False
        if not ip_ok:
            raise ResourceNotFound("Not found.")

        key = getattr(settings, "ADMIN_OPS_KEY", "") or ""
        supplied = request.headers.get("X-Admin-Key", "") or ""
        # Compare as bytes: compare_digest on str raises on non-ASCII,
        # which would 500 and stand out from the uniform 404s.
        if not key or not secrets.compare_digest(
            supplied.encode("utf-8", "replace"), key.encode("utf-8", "replace"),
        ):
            audit_log(
                "admin_ops.denied", action="read",
                metadata={"path": request.path, "ip": ip}, success=False,
            )
            raise ResourceNotFound("Not found.")

        now = time.monotonic()
        window = self._requests[ip]
        while window and now - window[0] > self._WINDOW_S:
            window.popleft()
        if len(window) >= self._MAX_PER_WINDOW:
            raise RateLimited()
        window.append(now)

        audit_log(
            "admin_ops.request", action="read",
            metadata={"path": request.path, "ip": ip},
        )


def _iso(dt):
    return dt.isoformat() if dt else None


def _page_params(request, default=50, maximum=200):
    try:
        limit = min(max(int(request.query_params.get("limit", default)), 1), maximum)
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = default, 0
    return limit, offset


def _search_params(request):
    """``(text, uuid_or_None)``: the search box matches emails and names by
    substring and ids exactly (a full UUID)."""
    search = (request.query_params.get("search") or "").strip()[:200]
    try:
        as_uuid = uuid.UUID(search) if search else None
    except ValueError:
        as_uuid = None
    return search, as_uuid


def _plan_name(plan) -> str:
    from core.utils.constants import PLAN_NAMES

    return PLAN_NAMES.get(plan, str(plan))


def _plan_seat_limit(plan) -> int:
    """``team_members`` for a plan key (-1 = unlimited)."""
    from core.utils.constants import PLAN_LIMITS, Plan, legacy_plan_key

    limits = PLAN_LIMITS.get(legacy_plan_key(plan)) or PLAN_LIMITS[Plan.FREE]
    raw = limits.get("team_members", -1)
    return int(raw) if isinstance(raw, int) else -1


def _first_memberships(user_ids) -> dict:
    """``{user_id: OrganizationMember}``: each user's earliest membership,
    the one plan resolution (core.utils.constants._org_for) uses."""
    from apps.accounts.models import OrganizationMember

    out: dict = {}
    for member in (
        OrganizationMember.objects.filter(user_id__in=list(user_ids))
        .select_related("organization")
        .order_by("created_at", "id")
    ):
        out.setdefault(member.user_id, member)
    return out


def _user_plan(sub, membership):
    """``(plan, source)`` exactly as billing resolves it: an org member
    (org features on) gets the org's plan; everyone else the plan their
    own subscription grants (Free without an active/trialing one)."""
    from apps.billing.services.plan_limits import plan_for_org, plan_for_subscription
    from core.permissions.org import org_features_enabled

    if membership is not None and org_features_enabled():
        return str(plan_for_org(membership.organization)), "organization"
    return str(plan_for_subscription(sub)), "subscription"


def _subscription(user):
    from apps.billing.models import Subscription

    try:
        return user.subscription
    except Subscription.DoesNotExist:
        return None


def _live_invites_q(prefix, now):
    return Q(**{
        f"{prefix}accepted_at__isnull": True,
        f"{prefix}revoked_at__isnull": True,
        f"{prefix}expires_at__gt": now,
    })


class AdminOverviewView(InternalAdminView):
    """Headline numbers for the admin Overview tab."""

    def get(self, request):
        from apps.accounts.models import User
        from apps.billing.models import Subscription
        from apps.llm_ranking.models import LLMRankingAudit
        from apps.websites.models import Website
        from core.utils.constants import SubscriptionStatus

        now = timezone.now()
        week = now - timedelta(days=7)
        month = now - timedelta(days=30)

        users = User.objects.all()
        subs = Subscription.objects.all()
        audits = LLMRankingAudit.objects.all()

        return Response({
            "users": {
                "total": users.count(),
                "new_7d": users.filter(created_at__gte=week).count(),
                "new_30d": users.filter(created_at__gte=month).count(),
                "verified": users.filter(is_email_verified=True).count(),
            },
            "subscriptions": {
                "active": subs.filter(status=SubscriptionStatus.ACTIVE).count(),
                "trialing": subs.filter(status=SubscriptionStatus.TRIALING).count(),
                "past_due": subs.filter(status=SubscriptionStatus.PAST_DUE).count(),
                "canceled": subs.filter(status=SubscriptionStatus.CANCELED).count(),
            },
            "projects": {"total": Website.objects.count()},
            "audits": {
                "total": audits.count(),
                "last_7d": audits.filter(created_at__gte=week).count(),
            },
            "ai_usage": _ai_usage_totals(month),
            "totals": _console_totals(now),
            "generated_at": now.isoformat(),
        })


def _ai_usage_totals(since):
    from django.db.models import Sum

    from apps.accounts.models import AITokenUsage

    qs = AITokenUsage.objects.filter(created_at__gte=since)
    agg = qs.aggregate(tokens=Sum("total_tokens"), cost=Sum("estimated_cost_usd"))
    return {
        "tokens_30d": int(agg["tokens"] or 0),
        "cost_30d_usd": float(agg["cost"] or 0),
    }


def _console_totals(now):
    """The console overview. Every number reconciles with the lists:
    ``seats_used`` is the sum of the accounts' ``seats_used`` and
    ``tokens`` / ``cost_usd`` the sum of the users' (attributed ledger
    rows); ledger rows with no user are reported as ``unattributed``."""
    from apps.accounts.models import Invitation, Organization, OrganizationMember, User
    from apps.accounts.services.token_usage import month_start_utc, usage_totals

    month_start = month_start_utc(now)
    individual = User.objects.filter(org_memberships__isnull=True).count()
    organizations = Organization.objects.count()
    usage = usage_totals(month_start)
    return {
        "period": {"month_start": month_start.isoformat(), "as_of": now.isoformat()},
        "users": User.objects.count(),
        "organizations": organizations,
        "individual_accounts": individual,
        "accounts": organizations + individual,
        "seats_used": OrganizationMember.objects.count() + individual,
        "pending_invites": Invitation.objects.filter(_live_invites_q("", now)).count(),
        "tokens": usage["attributed"]["tokens"],
        "cost_usd": usage["attributed"]["cost_usd"],
        "unattributed": usage["unattributed"],
    }


def _page_enrichment(user_ids, usage):
    """Per-user rollups for one page of the directory, computed as a few
    grouped queries over the page's ids — no join multiplication, no N+1.
    ``usage`` is the page's token_usage.usage_by_user map."""
    from django.db.models import Count

    from apps.accounts.services.token_usage import empty_usage
    from apps.llm_ranking.models import LLMRankingAudit
    from apps.prompt_library.models import BrandPrompt
    from apps.websites.models import Integration, Website

    prompts = {
        row["website__user_id"]: row["n"]
        for row in BrandPrompt.objects.filter(website__user_id__in=user_ids)
        .values("website__user_id")
        .annotate(n=Count("id"))
    }
    audits = {
        row["created_by_id"]: row["n"]
        for row in LLMRankingAudit.objects.filter(created_by_id__in=user_ids)
        .values("created_by_id")
        .annotate(n=Count("id"))
    }
    integrations: dict = {}
    for row in (
        Integration.objects
        .filter(website__user_id__in=user_ids, is_active=True)
        .values_list("website__user_id", "type")
    ):
        integrations.setdefault(row[0], set()).add(row[1])
    for uid in (
        Website.objects
        .filter(user_id__in=user_ids, pixel_verified=True)
        .values_list("user_id", flat=True)
    ):
        integrations.setdefault(uid, set()).add("pixel")

    def build(uid):
        u = usage.get(uid) or empty_usage()
        return {
            "prompts": prompts.get(uid, 0),
            "audits": audits.get(uid, 0),
            # Kept for the ftb-min dashboard; same ledger as ``tokens``.
            "tokens_total": u["tokens"]["all_time"]["total"],
            "ai_cost_usd": float(u["cost_usd"]["all_time"]),
            "integrations": sorted(integrations.get(uid, set())),
            "tokens": u["tokens"],
            "cost_usd": u["cost_usd"],
        }

    return build


class AdminUsersView(InternalAdminView):
    """Paginated user directory: identity, plan, organization and token
    usage (this UTC calendar month and all time) per user.

    Query: ``search`` (email or name substring, or an exact user id),
    ``limit`` (1-200, default 50), ``offset``.
    """

    def get(self, request):
        from apps.accounts.models import User
        from apps.accounts.services.token_usage import month_start_utc, usage_by_user

        now = timezone.now()
        month_start = month_start_utc(now)
        qs = (
            User.objects
            .select_related("subscription")
            .annotate(
                projects_count=Count(
                    "websites", filter=Q(websites__is_deleted=False),
                ),
            )
            .order_by("-created_at", "id")
        )
        search, as_uuid = _search_params(request)
        if search:
            match = Q(email__icontains=search) | Q(full_name__icontains=search)
            if as_uuid is not None:
                match |= Q(id=as_uuid)
            qs = qs.filter(match)
        limit, offset = _page_params(request)

        total = qs.count()
        page = list(qs[offset:offset + limit])
        ids = [u.id for u in page]
        usage = usage_by_user(ids, month_start)
        memberships = _first_memberships(ids)
        enrich = _page_enrichment(ids, usage)
        rows = []
        for user in page:
            sub = _subscription(user)
            membership = memberships.get(user.id)
            plan, plan_source = _user_plan(sub, membership)
            rows.append({
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "plan": plan,
                "plan_name": _plan_name(plan),
                "plan_source": plan_source,
                "subscription_status": sub.status if sub else None,
                "projects": user.projects_count,
                "active": user.is_active,
                "verified": user.is_email_verified,
                "joined": _iso(user.created_at),
                "last_login": _iso(user.last_login),
                "organization": (
                    {
                        "id": str(membership.organization_id),
                        "name": membership.organization.name,
                        "role": membership.role,
                    }
                    if membership is not None else None
                ),
                **enrich(user.id),
            })
        return Response({
            "total": total,
            "limit": limit,
            "offset": offset,
            "period": {"month_start": month_start.isoformat(), "as_of": now.isoformat()},
            "rows": rows,
        })


def _seat_row(user, *, role, joined_via, joined_at, usage):
    return {
        "user_id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": role,
        "joined_via": joined_via,
        "joined_at": _iso(joined_at),
        "active": user.is_active,
        "last_login": _iso(user.last_login),
        "tokens": usage["tokens"],
        "cost_usd": usage["cost_usd"],
    }


def _seat_numbers(limit, used, pending):
    reserved = used + pending
    return {
        "seat_limit": limit,
        "seats_used": used,
        "pending_invites": pending,
        "seats_available": max(limit - reserved, 0) if limit > 0 else -1,
        "over_limit": limit > 0 and used > limit,
    }


class AdminAccountsView(InternalAdminView):
    """Accounts with their seats: every organization, then every user who
    belongs to none (a one-seat ``individual`` account), so seats and their
    usage are shown for everyone.

    Query: ``type`` (``organization`` | ``individual``; default both),
    ``search`` (org name or member email/name substring, or an exact org
    or user id), ``limit`` (1-100, default 25), ``offset``. Organizations
    come first (newest first), then individual accounts (newest first).
    """

    def get(self, request):
        from apps.accounts.models import Organization, OrganizationMember, User
        from apps.accounts.services.token_usage import (
            add_usage,
            empty_usage,
            month_start_utc,
            usage_by_user,
        )
        from apps.billing.services.org_entitlements import seat_limit_for
        from apps.billing.services.plan_limits import plan_for_org, plan_for_subscription

        now = timezone.now()
        month_start = month_start_utc(now)
        kind = (request.query_params.get("type") or "").strip()
        search, as_uuid = _search_params(request)
        limit, offset = _page_params(request, default=25, maximum=100)

        orgs = (
            Organization.objects
            .select_related("owner")
            .annotate(
                members_count=Count("members", distinct=True),
                pending_count=Count(
                    "invitations",
                    filter=_live_invites_q("invitations__", now),
                    distinct=True,
                ),
            )
            .order_by("-created_at", "id")
        )
        solos = (
            User.objects.filter(org_memberships__isnull=True)
            .select_related("subscription")
            .order_by("-created_at", "id")
        )
        if search:
            text = (
                Q(email__icontains=search) | Q(full_name__icontains=search)
            )
            user_match = (text | Q(id=as_uuid)) if as_uuid is not None else text
            member_orgs = OrganizationMember.objects.filter(
                user__in=User.objects.filter(user_match),
            ).values("organization_id")
            org_match = Q(name__icontains=search) | Q(id__in=member_orgs)
            if as_uuid is not None:
                org_match |= Q(id=as_uuid)
            orgs = orgs.filter(org_match)
            solos = solos.filter(user_match)
        if kind == "organization":
            solos = solos.none()
        elif kind == "individual":
            orgs = orgs.none()

        org_total = orgs.count()
        solo_total = solos.count()
        org_page = list(orgs[offset:offset + limit]) if offset < org_total else []
        solo_offset = max(offset - org_total, 0)
        solo_room = limit - len(org_page)
        solo_page = list(solos[solo_offset:solo_offset + solo_room]) if solo_room else []

        members_by_org: dict = {}
        for member in (
            OrganizationMember.objects
            .filter(organization_id__in=[o.id for o in org_page])
            .select_related("user")
            .order_by("created_at", "id")
        ):
            members_by_org.setdefault(member.organization_id, []).append(member)
        user_ids = [m.user_id for ms in members_by_org.values() for m in ms]
        user_ids += [u.id for u in solo_page]
        usage = usage_by_user(user_ids, month_start)

        rows = []
        for org in org_page:
            seats = [
                _seat_row(
                    m.user, role=m.role, joined_via=m.joined_via,
                    joined_at=m.created_at,
                    usage=usage.get(m.user_id) or empty_usage(),
                )
                for m in members_by_org.get(org.id, [])
            ]
            plan = str(plan_for_org(org))
            rows.append({
                "type": "organization",
                "id": str(org.id),
                "name": org.name,
                "plan": plan,
                "plan_name": _plan_name(plan),
                "seat_limit_source": "contract" if org.seat_limit else "plan",
                **_seat_numbers(seat_limit_for(org), org.members_count, org.pending_count),
                "owner": {
                    "id": str(org.owner_id),
                    "email": org.owner.email,
                    "full_name": org.owner.full_name,
                },
                "created_at": _iso(org.created_at),
                **add_usage(s for s in seats),
                "seats": seats,
            })
        for user in solo_page:
            sub = _subscription(user)
            plan = str(plan_for_subscription(sub))
            u = usage.get(user.id) or empty_usage()
            seat = _seat_row(
                user, role="owner", joined_via="individual",
                joined_at=user.created_at, usage=u,
            )
            rows.append({
                "type": "individual",
                "id": str(user.id),
                "name": user.full_name or user.email,
                "plan": plan,
                "plan_name": _plan_name(plan),
                "seat_limit_source": "plan",
                **_seat_numbers(_plan_seat_limit(plan), 1, 0),
                "owner": {
                    "id": str(user.id),
                    "email": user.email,
                    "full_name": user.full_name,
                },
                "created_at": _iso(user.created_at),
                "tokens": u["tokens"],
                "cost_usd": u["cost_usd"],
                "seats": [seat],
            })
        return Response({
            "total": org_total + solo_total,
            "organizations": org_total,
            "individual_accounts": solo_total,
            "limit": limit,
            "offset": offset,
            "period": {"month_start": month_start.isoformat(), "as_of": now.isoformat()},
            "rows": rows,
        })
