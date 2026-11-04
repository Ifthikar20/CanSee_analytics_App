"""
Plan metadata exposed via the API — derived from core.utils.constants
so pricing/limits exist in exactly ONE place. (This module previously
carried its own divergent price table; that drift is gone.)

Feature lines list only what the code enforces or provides: the tracked
prompts, the engines Autopilot asks, Google page one, the daily text, the
AI spend allowance, the project cap and the org features. Built per request
from the configured engines, the Google provider and the SMS switch, so a
card never promises what the server cannot do.

The list shows Free, the two self-serve plans and Business. Pro ($45, the
2026-08 plan) is kept for its subscribers: ``get_plan("pro")`` still
resolves it, but it is never offered.

For feature-level gating, use constants.PLAN_LIMITS / plan_limits.py.
"""
from django.conf import settings

from core.ai_tracking import AI_SPEND_CAP_RATIO
from core.utils.constants import PLAN_LIMITS, PLAN_NAMES, Plan

# The plans the pricing window offers, in order.
SELF_SERVE_PLANS = (Plan.PROMPTS_100, Plan.PROMPTS_200)


def _count(n: int, noun: str) -> str:
    if n < 0:
        return f"Unlimited {noun}s"
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _usd(amount: float) -> str:
    return f"${amount:,.2f}".replace(".00", "")


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _limits(limits: dict) -> dict:
    return {
        "projects": limits["projects"],
        "monthly_prompts": limits["monthly_prompts"],
        "tracked_prompts": limits["tracked_prompts"],
        "autopilot": bool(limits["autopilot"]),
        "sms_digest": bool(limits["sms_digest"]),
    }


def _engine_labels() -> list[str]:
    """Labels of the AI engines that have a key on this server, in display
    order."""
    from apps.llm_ranking.providers import PROVIDERS
    from apps.llm_ranking.services.answer_window import ENGINE_ORDER
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS, is_configured

    return [
        MODEL_LABELS.get(provider, provider)
        for provider in ENGINE_ORDER
        if provider in PROVIDERS and is_configured(PROVIDERS[provider])
    ]


def _google_configured() -> bool:
    from apps.citations.services import serp_google

    return serp_google.is_configured()


def _texts_enabled() -> bool:
    from apps.notifications.services import sms_service

    return sms_service.is_enabled()


def _free() -> dict:
    limits = PLAN_LIMITS[Plan.FREE]
    features = [
        _count(limits["projects"], "project"),
        _count(limits["tracked_prompts"], "tracked prompt"),
    ]
    free_cap = float(getattr(settings, "AI_FREE_MONTHLY_CAP_USD", 0) or 0)
    if free_cap > 0:
        features.append(f"{_usd(free_cap)} of AI usage a month")
    return {
        "id": "free",
        "name": PLAN_NAMES[Plan.FREE],
        "self_serve": False,
        "segment": str(limits["segment"]),
        "price_monthly": 0,
        "price_yearly": 0,
        "interval": "month",
        "trial_days": 0,
        "features": features,
        "limits": _limits(limits),
    }


def _paid(plan: str) -> dict:
    """One of the two self-serve plans: 100 or 200 tracked prompts."""
    limits = PLAN_LIMITS[plan]
    features = [_count(limits["tracked_prompts"], "tracked prompt")]
    engines = _engine_labels()
    if engines:
        features.append(f"{_join(engines)}, asked every day")
    if _google_configured():
        # The default cadence a new project starts with (Autopilot settings
        # can change it per project): weekly unless the server says daily.
        daily = getattr(settings, "AUTOPILOT_GOOGLE_CADENCE", "weekly") == "daily"
        features.append(f"Google page one, checked every {'day' if daily else 'week'}")
    if _texts_enabled():
        features.append("One text a day with your results")
    features.append(f"{_usd(limits['price_monthly'] * AI_SPEND_CAP_RATIO)} of AI usage a month")
    features.append(_count(limits["projects"], "project"))
    return {
        "id": str(plan),
        "name": PLAN_NAMES[plan],
        "self_serve": True,
        "segment": str(limits["segment"]),
        "price_monthly": limits["price_monthly"],
        "price_yearly": limits["price_yearly"],
        "interval": "month",
        "trial_days": limits["trial_days"],
        "features": features,
        "limits": _limits(limits),
    }


def _pro() -> dict:
    limits = PLAN_LIMITS[Plan.PRO]
    features = [
        _count(limits["projects"], "project"),
        f"{_count(limits['monthly_prompts'], 'prompt')} a month",
        f"{_usd(limits['price_monthly'] * AI_SPEND_CAP_RATIO)} of AI usage a month",
    ]
    if limits["trial_days"]:
        free_projects = PLAN_LIMITS[Plan.FREE]["projects"]
        features.append(
            f"{_count(free_projects, 'project')} during the "
            f"{limits['trial_days']}-day trial"
        )
    return {
        "id": "pro",
        "name": PLAN_NAMES[Plan.PRO],
        "self_serve": False,
        "segment": str(limits["segment"]),
        "price_monthly": limits["price_monthly"],
        "price_yearly": limits["price_yearly"],
        "interval": "month",
        "trial_days": limits["trial_days"],
        "features": features,
        "limits": _limits(limits),
    }


def _business() -> dict:
    limits = PLAN_LIMITS[Plan.BUSINESS]
    return {
        "id": "business",
        "name": PLAN_NAMES[Plan.BUSINESS],
        "self_serve": False,
        "segment": str(limits["segment"]),
        "price_monthly": limits["price_monthly"],  # -1 = custom
        "price_yearly": limits["price_yearly"],
        # Custom contracts: no self-serve billing interval.
        "interval": None,
        "trial_days": limits["trial_days"],
        "features": [
            _count(limits["projects"], "project"),
            "An organization workspace with team seats",
            f"{_count(limits['monthly_prompts'], 'prompt')} a month per seat, "
            "or the amount in your contract",
            "AI usage allowance set in your contract",
            "Single sign-on (SSO)",
        ],
        "limits": _limits(limits),
    }


def _plans() -> list:
    """The listed plans, in order. Pro is left out (see the module doc)."""
    return [_free(), *(_paid(plan) for plan in SELF_SERVE_PLANS), _business()]


# Legacy plan name → live-tier mapping
_LEGACY_MAP = {
    "individual": "pro",
    "starter": "pro",
    "growth": "pro",
    "scale": "business",
    "team": "business",
    "enterprise": "business",
}


class PlanService:
    @staticmethod
    def get_all_plans() -> list:
        return _plans()

    @staticmethod
    def get_plan(plan_id: str) -> dict | None:
        resolved = _LEGACY_MAP.get(plan_id, plan_id)
        if resolved == Plan.PRO:
            return _pro()
        return next((p for p in _plans() if p["id"] == resolved), None)

    @staticmethod
    def get_plan_for_segment(segment: str) -> dict | None:
        return next((p for p in _plans() if p["segment"] == segment), None)
