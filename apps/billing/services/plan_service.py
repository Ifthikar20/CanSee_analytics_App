"""
Plan metadata exposed via the API — derived from core.utils.constants
so pricing/limits exist in exactly ONE place. (This module previously
carried its own divergent price table; that drift is gone.)

Feature lines list only what the code enforces or provides: the project
cap, the monthly prompt allowance, the AI spend allowance and the org
features. Built per request so they follow the settings they read.

For feature-level gating, use constants.PLAN_LIMITS / plan_limits.py.
"""
from django.conf import settings

from core.ai_tracking import AI_SPEND_CAP_RATIO
from core.utils.constants import PLAN_LIMITS, Plan


def _count(n: int, noun: str) -> str:
    if n < 0:
        return f"Unlimited {noun}s"
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _usd(amount: float) -> str:
    return f"${amount:,.2f}".replace(".00", "")


def _limits(limits: dict) -> dict:
    return {
        "projects": limits["projects"],
        "monthly_prompts": limits["monthly_prompts"],
    }


def _free() -> dict:
    limits = PLAN_LIMITS[Plan.FREE]
    features = [
        _count(limits["projects"], "project"),
        f"{_count(limits['monthly_prompts'], 'prompt')} a month",
    ]
    free_cap = float(getattr(settings, "AI_FREE_MONTHLY_CAP_USD", 0) or 0)
    if free_cap > 0:
        features.append(f"{_usd(free_cap)} of AI usage a month")
    return {
        "id": "free",
        "name": "Free",
        "segment": str(limits["segment"]),
        "price_monthly": 0,
        "price_yearly": 0,
        "trial_days": 0,
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
        "name": "Pro",
        "segment": str(limits["segment"]),
        "price_monthly": limits["price_monthly"],
        "price_yearly": limits["price_yearly"],
        "trial_days": limits["trial_days"],
        "features": features,
        "limits": _limits(limits),
    }


def _business() -> dict:
    limits = PLAN_LIMITS[Plan.BUSINESS]
    return {
        "id": "business",
        "name": "Business",
        "segment": str(limits["segment"]),
        "price_monthly": limits["price_monthly"],  # -1 = custom
        "price_yearly": limits["price_yearly"],
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
    return [_free(), _pro(), _business()]


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
        return next((p for p in _plans() if p["id"] == resolved), None)

    @staticmethod
    def get_plan_for_segment(segment: str) -> dict | None:
        return next((p for p in _plans() if p["segment"] == segment), None)
