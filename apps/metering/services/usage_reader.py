"""
The read model behind every usage surface (Settings page, Billing page,
chat `usage` command).

One entry point — get_period_usage(user) — returns a JSON-safe dict:

  - window   = the user's current billing period (periods.billing_period_for)
  - totals + daily token series from Polar meter quantities when
    POLAR_READS_ENABLED and reachable (source="polar"), otherwise from
    the local AITokenUsage ledger (source="local"). Breakdowns
    (module/model/provider/role) always come from the local ledger:
    Polar quantities cannot group by metadata.
  - allowance = the honest denominator: the plan's USD spend cap and the
    period's real spend/tokens. The old fabricated `capacity_tokens`
    (dollar cap times observed tokens-per-dollar) is gone — tokens are
    reported as real counts, never as an invented allowance.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

from apps.metering.services.periods import Period, billing_period_for

logger = logging.getLogger("metering.reader")

_CACHE_SECONDS = 60


def period_spent_usd(user, period: Period | None = None) -> float:
    """Ledger spend for the user's current billing period (authoritative)."""
    from django.db.models import Sum

    from apps.accounts.models import AITokenUsage

    period = period or billing_period_for(user)
    total = (
        AITokenUsage.objects.filter(user=user, created_at__gte=period.start)
        .aggregate(total=Sum("estimated_cost_usd"))["total"]
        or 0
    )
    return float(total)


def allowance_status(user, period: Period | None = None, used_tokens: int | None = None) -> dict:
    """The allowance block shared by every consumer (replaces _cap_status).

    ``plan`` names the plan the cap comes from (an org member's org plan,
    else the user's own), so a usage line never reads "free" next to an
    organization's allowance.
    """
    from core.ai_tracking import (
        AI_SPEND_CAP_RATIO,
        AI_SPEND_WARN_RATIO,
        ai_allowance_plan,
        effective_ai_cap,
    )

    period = period or billing_period_for(user)
    spent = period_spent_usd(user, period)
    cap = effective_ai_cap(user)
    plan = str(ai_allowance_plan(user))

    manual = float(getattr(user, "monthly_ai_cost_cap_usd", 0) or 0)
    if cap <= 0:
        source = "none"
    elif manual > 0 and cap == manual:
        source = "manual"
    else:
        source = "plan"

    if used_tokens is None:
        from django.db.models import Sum

        from apps.accounts.models import AITokenUsage

        used_tokens = (
            AITokenUsage.objects.filter(user=user, created_at__gte=period.start)
            .aggregate(total=Sum("total_tokens"))["total"]
            or 0
        )

    return {
        "plan": plan,
        "cap_usd": cap,
        "cap_source": source,
        "cap_ratio": AI_SPEND_CAP_RATIO,
        "spent_usd": round(spent, 4),
        "pct_used": round(spent / cap * 100, 1) if cap > 0 else None,
        "used_tokens": int(used_tokens),
        "warning": cap > 0 and spent >= cap * AI_SPEND_WARN_RATIO,
        "exceeded": cap > 0 and spent >= cap,
        "period_start": period.start.isoformat(),
        "resets_at": period.end.isoformat(),
    }


def get_period_usage(user) -> dict:
    """Usage summary for the user's current billing period. Never raises."""
    period = billing_period_for(user)
    cache_key = f"usage_summary:{user.id}:{period.key}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    local = _local_summary(user, period)
    result = {
        "source": "local",
        "period": {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "source": period.source,
        },
        **local,
    }

    polar_part = _polar_totals(user, period)
    if polar_part is not None:
        polar_total, polar_daily = polar_part
        result["source"] = "polar"
        result["totals"]["total_tokens"] = polar_total
        result["daily"] = _merge_daily(local["daily"], polar_daily)

    result["allowance"] = allowance_status(
        user, period, used_tokens=result["totals"]["total_tokens"]
    )

    try:
        cache.set(cache_key, result, _CACHE_SECONDS)
    except Exception:
        pass
    return result


# ── Local ledger aggregation ──────────────────────────────────────────────


def _local_summary(user, period: Period) -> dict:
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncDate

    from apps.accounts.models import AITokenUsage

    qs = AITokenUsage.objects.filter(user=user, created_at__gte=period.start)

    totals = qs.aggregate(
        total_calls=Count("id"),
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_tokens=Sum("total_tokens"),
        total_cost=Sum("estimated_cost_usd"),
    )

    def _rows(values_field, out_key):
        rows = list(
            qs.values(values_field).annotate(
                calls=Count("id"),
                tokens=Sum("total_tokens"),
                cost=Sum("estimated_cost_usd"),
            ).order_by("-tokens")
        )
        return [
            {
                out_key: r[values_field],
                "calls": r["calls"],
                "tokens": r["tokens"] or 0,
                "cost": float(r["cost"] or 0),
            }
            for r in rows
        ]

    by_module = list(
        qs.values("module").annotate(
            calls=Count("id"),
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        ).order_by("-tokens")
    )
    for r in by_module:
        r["cost"] = float(r["cost"] or 0)

    by_role_map: dict = {}
    for row in qs.values("metadata", "total_tokens", "estimated_cost_usd"):
        role = (row["metadata"] or {}).get("role") or "upstream"
        bucket = by_role_map.setdefault(
            role, {"role": role, "calls": 0, "tokens": 0, "cost": 0.0}
        )
        bucket["calls"] += 1
        bucket["tokens"] += row["total_tokens"] or 0
        bucket["cost"] += float(row["estimated_cost_usd"] or 0)
    by_role = sorted(by_role_map.values(), key=lambda r: r["cost"], reverse=True)
    for r in by_role:
        r["cost"] = round(r["cost"], 6)

    daily = [
        {
            "day": r["day"].isoformat(),
            "calls": r["calls"],
            "tokens": r["tokens"] or 0,
            "cost": float(r["cost"] or 0),
        }
        for r in qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(calls=Count("id"), tokens=Sum("total_tokens"), cost=Sum("estimated_cost_usd"))
        .order_by("day")
    ]

    return {
        "totals": {
            "calls": totals["total_calls"] or 0,
            "input_tokens": totals["total_input"] or 0,
            "output_tokens": totals["total_output"] or 0,
            "total_tokens": totals["total_tokens"] or 0,
            "estimated_cost_usd": float(totals["total_cost"] or 0),
        },
        "by_module": by_module,
        "by_model": _rows("model_name", "model_name"),
        "by_provider": _rows("provider", "provider"),
        "by_role": by_role,
        "daily": daily,
    }


# ── Polar meter reads ─────────────────────────────────────────────────────


def _polar_totals(user, period: Period):
    """(total_tokens, {day_iso: tokens}) from the Polar tokens meter, or
    None when reads are disabled/unconfigured/unreachable.

    Polar is only authoritative for users that EXIST in Polar. Users the
    API permanently rejected (e.g. dev accounts on undeliverable email
    domains) have no PolarCustomer marker; their usage lives on the
    local ledger only, and reading Polar for them would honestly return
    0 — which is the wrong answer. Those users stay on local reads."""
    from django.conf import settings

    from apps.metering import polar_client
    from apps.metering.models import PolarCustomer

    if not getattr(settings, "POLAR_READS_ENABLED", False):
        return None
    meter_id = getattr(settings, "POLAR_METER_TOKENS_ID", "")
    if not meter_id or not polar_client.is_configured():
        return None
    if not PolarCustomer.objects.filter(
        user=user,
        environment=settings.POLAR_ENVIRONMENT,
    ).exclude(polar_customer_id="").exists():
        return None

    try:
        resp = polar_client.meter_quantities(
            meter_id,
            start=period.start,
            end=min(period.end, timezone.now()),
            interval="day",
            external_customer_id=str(user.id),
        )
    except polar_client.PolarRejected:
        logger.warning("polar meter read rejected; using local ledger", exc_info=True)
        return None
    except polar_client.PolarUnavailable:
        logger.info("polar unreachable; using local ledger")
        return None
    except Exception:
        logger.warning("polar meter read failed; using local ledger", exc_info=True)
        return None

    daily = {}
    for q in getattr(resp, "quantities", []) or []:
        ts = getattr(q, "timestamp", None)
        qty = getattr(q, "quantity", 0) or 0
        if ts is not None:
            daily[ts.date().isoformat()] = int(qty)
    total = int(getattr(resp, "total", 0) or 0)
    return total, daily


def _merge_daily(local_daily: list[dict], polar_daily: dict) -> list[dict]:
    """Polar token counts joined with local calls/cost per day."""
    by_day = {row["day"]: dict(row) for row in local_daily}
    for day, tokens in polar_daily.items():
        row = by_day.setdefault(day, {"day": day, "calls": 0, "tokens": 0, "cost": 0.0})
        row["tokens"] = tokens
    return [by_day[d] for d in sorted(by_day)]
