"""
Unified AI token usage tracking.

Provides a single function `record_usage()` that all AI call sites use to log
their token consumption. This enables centralized monitoring and billing.

The AITokenUsage model lives in apps.accounts.models but is accessed through
this module for convenience.
"""
import logging
import uuid
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Pricing (per 1M tokens) ──
# Sources:
#   Anthropic — https://docs.anthropic.com/en/docs/about-claude/models
#   OpenAI    — https://openai.com/api/pricing
#   Google    — https://ai.google.dev/pricing
#   Perplexity — https://docs.perplexity.ai/docs/pricing
PRICING = {
    # Anthropic. Optional cache rates: cache_write_5m / cache_write_1h /
    # cache_read; a row without them prices cache tokens at 1.25x / 2x /
    # 0.1x of "input" (Anthropic's standard multipliers).
    "claude-opus-5-5": {
        "input": 4.00, "output": 20.00,
        "cache_write_5m": 5.00, "cache_write_1h": 8.00, "cache_read": 0.20,
    },
    "claude-opus-5": {
        "input": 5.00, "output": 25.00,
        "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50,
    },
    "claude-opus-4-8": {
        "input": 5.00, "output": 25.00,
        "cache_write_5m": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50,
    },
    # The agent's research-agent analyses (AGENT_WORKER_MODEL). These are
    # the rates for prompts of at most 100K tokens; above that the model
    # bills $0.50 / $2.50, which this table does not model, so every caller
    # keeps its input far below (a worker request carries at most 24,000
    # characters of content).
    "claude-haiku-5-5": {
        "input": 0.10, "output": 0.50,
        "cache_write_5m": 0.125, "cache_write_1h": 0.20, "cache_read": 0.01,
    },
    # Engine-check variants a tracked prompt can select
    # (apps.llm_ranking.providers.MODEL_VARIANTS).
    "claude-fable-5": {
        "input": 10.00, "output": 50.00,
        "cache_write_5m": 12.50, "cache_write_1h": 20.00, "cache_read": 1.00,
    },
    "claude-sonnet-5": {
        "input": 2.00, "output": 10.00,
        "cache_write_5m": 2.50, "cache_write_1h": 4.00, "cache_read": 0.20,
    },
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-opus-4-1-20250805": {"input": 15.00, "output": 75.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    # OpenAI
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.00},
    # Google
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    # Perplexity. Sonar bills a per-request web-search fee on top of
    # tokens; "per_call" models that as a flat USD adder per API call,
    # set at the high-context tier so the estimate can only overshoot.
    "sonar": {"input": 1.00, "output": 1.00, "per_call": 0.008},
    "sonar-pro": {"input": 3.00, "output": 15.00, "per_call": 0.014},
    # Legacy Sonar names kept so historical rows keep re-pricing correctly.
    "llama-3.1-sonar-small-128k-online": {"input": 0.20, "output": 0.20},
    "llama-3.1-sonar-large-128k-online": {"input": 1.00, "output": 1.00},
    # xAI
    "grok-4": {"input": 3.00, "output": 15.00},
    "grok-3": {"input": 3.00, "output": 15.00},
    # Fallback for models without a row. It is a mid-tier rate, so a model
    # priced above it would be understated against the margin cap: every
    # model the product selects on its own (provider defaults, MODEL_VARIANTS)
    # needs its own row rather than relying on this one.
    "default": {"input": 3.00, "output": 15.00},
}


# Map known model names to canonical provider keys used in AITokenUsage.provider.
MODEL_PROVIDER = {
    "claude-opus-5-5": "anthropic",
    "claude-opus-5": "anthropic",
    "claude-opus-4-8": "anthropic",
    "claude-haiku-5-5": "anthropic",
    "claude-fable-5": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-sonnet-4-20250514": "anthropic",
    "claude-sonnet-4-5-20250929": "anthropic",
    "claude-opus-4-1-20250805": "anthropic",
    "claude-3-5-sonnet-20241022": "anthropic",
    "claude-3-5-sonnet-latest": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5-20251001": "anthropic",
    "claude-haiku-4-5": "anthropic",
    "claude-3-haiku-20240307": "anthropic",
    "gpt-4o-mini": "openai",
    "gpt-4o": "openai",
    "gpt-4-turbo": "openai",
    "text-embedding-3-small": "openai",
    "gemini-1.5-flash": "google",
    "gemini-1.5-pro": "google",
    "gemini-2.0-flash": "google",
    "sonar": "perplexity",
    "sonar-pro": "perplexity",
    "llama-3.1-sonar-small-128k-online": "perplexity",
    "llama-3.1-sonar-large-128k-online": "perplexity",
    "grok-4": "xai",
    "grok-3": "xai",
}


# Anthropic server-side web search: $10 per 1,000 searches. Web fetch is
# billed as tokens only.
WEB_SEARCH_COST_PER_REQUEST_USD = 0.01

# Default cache-token rates as multiples of a row's "input" price.
_CACHE_RATE_MULTIPLIERS = {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1}


def _cache_rate(pricing: dict, key: str) -> float:
    rate = pricing.get(key)
    if rate is None:
        rate = pricing["input"] * _CACHE_RATE_MULTIPLIERS[key]
    return rate


def _estimate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    web_search_requests: int = 0,
) -> float:
    """Calculate estimated USD cost based on model pricing.

    ``input_tokens`` is the uncached input (Anthropic's ``input_tokens``);
    cache writes per TTL, cache reads and server web searches are priced
    separately.
    """
    pricing = PRICING.get(model_name, PRICING["default"])
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    cache_cost = 0.0
    if cache_write_5m or cache_write_1h or cache_read:
        cache_cost = (
            cache_write_5m * _cache_rate(pricing, "cache_write_5m")
            + cache_write_1h * _cache_rate(pricing, "cache_write_1h")
            + cache_read * _cache_rate(pricing, "cache_read")
        ) / 1_000_000
    search_cost = web_search_requests * WEB_SEARCH_COST_PER_REQUEST_USD
    # Some providers (Perplexity Sonar) bill a flat per-request fee for the
    # web-search step in addition to tokens.
    flat = pricing.get("per_call", 0.0)
    return round(input_cost + output_cost + cache_cost + search_cost + flat, 6)


def estimate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    web_search_requests: int = 0,
) -> float:
    """Public form of ``_estimate_cost`` for pricing outside this module."""
    return _estimate_cost(
        model_name, input_tokens, output_tokens,
        cache_write_5m=cache_write_5m, cache_write_1h=cache_write_1h,
        cache_read=cache_read, web_search_requests=web_search_requests,
    )


def record_usage(
    *,
    module: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    user=None,
    website=None,
    provider: str = "anthropic",
    duration_ms: int = 0,
    metadata: dict | None = None,
    idempotency_key: str | None = None,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    web_search_requests: int = 0,
):
    """
    Record an AI API call's token usage.

    Call this after every `client.messages.create()` using `response.usage`.

    Writes the AITokenUsage ledger row and, in the same transaction, a
    Polar outbox event (apps.metering) so external metering can never
    drift from the ledger. Pass ``idempotency_key`` from retryable call
    sites (Celery acks_late tasks): a replay under the same key collapses
    to the original row instead of double-counting. Without a key each
    call gets a unique one (no dedupe semantics).

    Prompt-cache tokens (writes per TTL, reads) and server web searches
    are priced into the cost and kept in ``metadata``; ``total_tokens``
    stays input + output.

    Example:
        from core.ai_tracking import record_usage

        response = client.messages.create(...)
        record_usage(
            module="llm_ranking",
            model_name="claude-sonnet-4-20250514",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            user=request.user,
        )
    """
    try:
        from django.db import transaction

        from apps.accounts.models import AITokenUsage

        total = input_tokens + output_tokens
        cost = _estimate_cost(
            model_name, input_tokens, output_tokens,
            cache_write_5m=cache_write_5m, cache_write_1h=cache_write_1h,
            cache_read=cache_read, web_search_requests=web_search_requests,
        )
        # Auto-derive provider when caller leaves the default — keeps every
        # row consistent for the centralized "by provider" rollup.
        if provider == "anthropic" and model_name in MODEL_PROVIDER:
            provider = MODEL_PROVIDER[model_name]

        metadata = dict(metadata or {})
        for name, value in (
            ("cache_write_5m_tokens", cache_write_5m),
            ("cache_write_1h_tokens", cache_write_1h),
            ("cache_read_tokens", cache_read),
            ("web_search_requests", web_search_requests),
        ):
            if value:
                metadata.setdefault(name, value)

        key = idempotency_key or uuid.uuid4().hex
        defaults = {
            "user": user,
            "website": website,
            "module": module,
            "provider": provider,
            "model_name": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
            "estimated_cost_usd": cost,
            "duration_ms": duration_ms,
            "metadata": metadata or {},
        }

        def _write_ledger():
            return AITokenUsage.objects.get_or_create(
                idempotency_key=key, defaults=defaults
            )

        try:
            with transaction.atomic():
                row, created = _write_ledger()
                if created:
                    from apps.metering.services.events import enqueue_usage_event

                    enqueue_usage_event(row)
        except Exception:
            # The ledger is enforcement-critical; metering is reconcilable
            # (polar_backfill). Keep the row even when the outbox insert
            # fails.
            logger.warning(
                "Polar outbox enqueue failed; retrying ledger write alone",
                exc_info=True,
            )
            row, created = _write_ledger()

        if not created:
            logger.info("Duplicate AI usage suppressed (idempotency_key=%s)", key)
            return

        from apps.metering.services.events import ingest_mode, schedule_flush

        if ingest_mode() != "off":
            transaction.on_commit(schedule_flush)

        logger.debug(
            "AI usage: %s | %s | %d in + %d out = %d tokens | $%.4f",
            module, model_name, input_tokens, output_tokens, total, cost,
        )

        if user is not None:
            from apps.metering.services import spend_counter

            spend_counter.bump(user, cost)
        elif module != "onboarding":
            # Pre-signup onboarding scans are the only sanctioned
            # unattributed spend; anything else is an attribution bug.
            logger.warning(
                "Unattributed AI usage recorded: module=%s model=%s", module, model_name
            )
    except Exception as e:
        logger.warning("Failed to record AI token usage: %s", e, exc_info=True)


def get_usage_summary(user=None, days=30):
    """
    Aggregated AI usage stats across every provider and module for a user.

    Powers the centralized "Overall Usage" panel in Settings. Each row recorded
    via record_usage() — Lead Finder, Messaging, Analytics, LLM Ranking
    (upstream + extraction), Competitor Discovery — rolls into the same totals.
    """
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncDate

    from apps.accounts.models import AITokenUsage

    cutoff = timezone.now() - timedelta(days=days)
    qs = AITokenUsage.objects.filter(created_at__gte=cutoff)
    if user:
        qs = qs.filter(user=user)

    totals = qs.aggregate(
        total_calls=Count("id"),
        total_input=Sum("input_tokens"),
        total_output=Sum("output_tokens"),
        total_tokens=Sum("total_tokens"),
        total_cost=Sum("estimated_cost_usd"),
    )

    by_module = list(
        qs.values("module").annotate(
            calls=Count("id"),
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        ).order_by("-tokens")
    )

    by_model = list(
        qs.values("model_name").annotate(
            calls=Count("id"),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        ).order_by("-tokens")
    )

    by_provider = list(
        qs.values("provider").annotate(
            calls=Count("id"),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        ).order_by("-cost")
    )

    # Role split — bucket every recorded call by metadata.role so the rows
    # add up to the totals row. Rows lacking a role tag (legacy records and
    # call sites that pre-date role tagging) bucket as "upstream".
    by_role_map: dict = {}
    for row in qs.values("metadata", "total_tokens", "estimated_cost_usd"):
        role = (row["metadata"] or {}).get("role") or "upstream"
        bucket = by_role_map.setdefault(
            role, {"role": role, "calls": 0, "tokens": 0, "cost": 0.0},
        )
        bucket["calls"] += 1
        bucket["tokens"] += row["total_tokens"] or 0
        bucket["cost"] += float(row["estimated_cost_usd"] or 0)
    by_role = sorted(by_role_map.values(), key=lambda r: r["cost"], reverse=True)
    for r in by_role:
        r["cost"] = round(r["cost"], 6)

    daily = list(
        qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            calls=Count("id"),
            tokens=Sum("total_tokens"),
            cost=Sum("estimated_cost_usd"),
        )
        .order_by("day")
    )

    # Allowance status — the honest denominator (plan USD cap + real
    # period spend/tokens). Computed over the user's billing period, NOT
    # the rolling window above.
    if user:
        from apps.metering.services.usage_reader import allowance_status

        cap_status = allowance_status(user)
    else:
        cap_status = None

    return {
        "period_days": days,
        "totals": {
            "calls": totals["total_calls"] or 0,
            "input_tokens": totals["total_input"] or 0,
            "output_tokens": totals["total_output"] or 0,
            "total_tokens": totals["total_tokens"] or 0,
            "estimated_cost_usd": float(totals["total_cost"] or 0),
        },
        "by_module": by_module,
        "by_model": by_model,
        "by_provider": by_provider,
        "by_role": by_role,
        "daily": daily,
        "cap_status": cap_status,
    }


# Share of a plan's monthly price a user may consume as AI cost. Above
# this the platform loses money on the account, so the cap is a hard
# stop, not advisory.
AI_SPEND_CAP_RATIO = 0.65
# Fraction of the allowance at which the user gets a warning notification.
AI_SPEND_WARN_RATIO = 0.80


def ai_allowance_plan(user) -> str:
    """The plan whose AI allowance ``user`` gets, as a PLAN_LIMITS key.

    The organization's plan for an org member (plan_limits.org_plan_for,
    which honours the ORG_FEATURES_ENABLED master switch), else the plan
    the user's own subscription grants (plan_limits.current_plan_for).
    effective_ai_cap prices this plan and every usage surface labels the
    cap with it, so the label and the number can never disagree.
    """
    from apps.billing.services.plan_limits import current_plan_for, org_plan_for
    from core.utils.constants import PLAN_LIMITS, Plan

    plan = org_plan_for(user) or current_plan_for(user)
    if plan not in PLAN_LIMITS:
        # An unrecognised plan value grants nothing beyond the free tier.
        plan = Plan.FREE
    return plan


def effective_ai_cap(user) -> float:
    """Monthly AI spend allowance in USD for ``user``. 0 = unlimited.

    The plan comes from ai_allowance_plan. Organization members get their
    organization's plan cap, resolved the same way plan_limits.get_limits
    resolves an org member's plan (plan_limits.org_plan_for); only an
    actual OrganizationMember row grants it, and only while
    ORG_FEATURES_ENABLED is on: with the master switch off production is
    plain B2C, so a member staged before launch keeps the individual
    allowance, as the session and billing pages show. Everyone else is
    resolved from ACTUAL subscription status (plan_limits.current_plan_for): an account
    without an active/trialing subscription is on the FREE plan and gets
    ``settings.AI_FREE_MONTHLY_CAP_USD`` (a small acquisition budget) —
    never a paid tier's allowance. The denormalized user.plan value is
    not consulted; it defaults to a paid tier and used to leak paid
    allowances (and the $500 ceiling that produced the fabricated
    305M-token display) to accounts outside any organization.
    PAYWALL_ENABLED does not enter into it: with the paywall off an
    individual without a subscription still has the free allowance,
    never an unlimited one.

    Paid plans derive the cap from price: Pro $45/mo allows
    $45 * 0.65 = $29.25 of model cost per billing period. A manual
    ``monthly_ai_cost_cap_usd`` only TIGHTENS a priced plan's cap, but
    RAISES a free account's (comped testers) and SETS the ceiling for
    Business/custom (negotiated), which otherwise uses the finite
    ``settings.AI_ENTERPRISE_MONTHLY_CAP_USD`` safety ceiling.
    """
    if user is None:
        return 0.0
    from django.conf import settings

    from core.utils.constants import PLAN_LIMITS, Plan

    plan = ai_allowance_plan(user)
    limits = PLAN_LIMITS[plan]
    manual = float(getattr(user, "monthly_ai_cost_cap_usd", 0) or 0)

    if plan == Plan.FREE:
        free_cap = float(getattr(settings, "AI_FREE_MONTHLY_CAP_USD", 0) or 0)
        return manual if manual > 0 else free_cap

    price = limits.get("price_monthly") or 0
    if price and price > 0:
        # Priced plan: cap derived from price; a manual cap only tightens it.
        derived = round(price * AI_SPEND_CAP_RATIO, 2)
        return min(manual, derived) if manual > 0 else derived

    # Business / custom pricing: no derived cap. Never unlimited by default.
    if manual > 0:
        return manual
    return float(getattr(settings, "AI_ENTERPRISE_MONTHLY_CAP_USD", 0) or 0)


def month_to_date_cost(user) -> float:
    """Billing-period-to-date AI cost for a user. Used by per-module cost
    guards and the hard spend wall.

    Kept under its historical name (many call sites import it). The
    window is apps.metering.services.periods.billing_period_for — today
    that is the calendar month for everyone (no populated subscription
    periods exist), identical to the old behavior; once Phase 2 populates
    real billing cycles, the wall and every usage surface move together.
    """
    from apps.metering.services.usage_reader import period_spent_usd

    return period_spent_usd(user)
