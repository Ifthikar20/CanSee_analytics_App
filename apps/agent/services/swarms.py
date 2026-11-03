"""Research agents ("swarms"): reads and re-runs (swarm_contract.md 6).

A swarm is private to the user whose chat planned it: every lookup here
filters on that user as well as the website, so another member of the
same project gets a 404. A re-run is a new version of the same swarm,
started as a new turn at the end of the chat's current branch
(services.runs.start_rerun); this module validates the scope and the
allowance and writes the user message's text.
"""
from __future__ import annotations

from decimal import Decimal

from apps.agent.services import runs
from apps.agent.tools.shaping import clip
from core.exceptions import ResourceNotFound

RERUN_PROMPT_CHARS = 60


def allowance_shortfall(user, need_usd) -> dict | None:
    """{need_usd, left_usd, cap_usd} when the AI allowance left this period
    is below ``need_usd``; None when it covers it or the cap is unlimited
    (0). A spent allowance is reported by the callers' own allowance check
    first (allowance_exceeded)."""
    from core.ai_tracking import effective_ai_cap, month_to_date_cost

    cap = float(effective_ai_cap(user))
    if cap <= 0:
        return None
    left = cap - float(month_to_date_cost(user))
    need = float(Decimal(str(need_usd)))
    if left >= need:
        return None
    return {"need_usd": need, "left_usd": left, "cap_usd": cap}


def get_swarm(user, website, swarm_id):
    """The user's swarm in ``website`` (a swarm of another user's chat is
    not found, never forbidden)."""
    from apps.agent.models import AgentSwarm

    swarm = (AgentSwarm.objects.select_related("chat")
             .filter(id=swarm_id, website=website, chat__user=user).first())
    if swarm is None:
        raise ResourceNotFound("Research not found.")
    return swarm


def list_versions(swarm) -> list:
    """Every version of ``swarm``, newest first, without their tables and
    results (``has_table`` says whether one exists)."""
    from django.db.models import BooleanField, ExpressionWrapper, Q

    from apps.agent.models import AgentSwarmVersion

    return list(
        AgentSwarmVersion.objects.filter(swarm=swarm)
        .defer("table", "results")
        .annotate(has_table=ExpressionWrapper(Q(table__isnull=False),
                                              output_field=BooleanField()))
        .order_by("-version")
    )


def get_version(swarm, number: int):
    from apps.agent.models import AgentSwarmVersion

    version = AgentSwarmVersion.objects.filter(swarm=swarm, version=number).first()
    if version is None:
        raise ResourceNotFound("Version not found.")
    return version


def prompt_texts(swarm) -> dict[str, str]:
    """Prompt key ("p1") -> prompt text, from the swarm's plan."""
    return {
        str(p.get("key")): str(p.get("text") or "")
        for p in (swarm.plan or {}).get("prompts") or []
        if isinstance(p, dict)
    }


def category_label(swarm, key: str) -> str:
    from apps.agent.constants import FINDING_CATEGORY_LABELS

    for category in swarm.categories or []:
        if isinstance(category, dict) and category.get("key") == key and category.get("label"):
            return str(category["label"])
    return FINDING_CATEGORY_LABELS.get(key, key)


def rerun_text(swarm, scope: str, scope_key: str, task_ids: list[str]) -> str:
    """The generated user message of a re-run (contract 6b, exact)."""
    if scope == "category":
        return f"Re-run the {category_label(swarm, scope_key)} agents"
    if scope == "task":
        spec = next((t for t in swarm.tasks or [] if t.get("id") == scope_key), {})
        prompt = prompt_texts(swarm).get(str(spec.get("subcategory") or ""), "")
        label = spec.get("label") or scope_key
        return f"Re-run the {label} agent for “{clip(prompt, RERUN_PROMPT_CHARS)}”"
    return f"Re-run all {len(task_ids)} research agents"


def start_rerun(user, website, swarm_id, *, scope: str, category: str | None = None,
                task_id: str | None = None, idempotency_key=None) -> runs.StartedRun:
    """Validate a re-run of one of the user's swarms and start it: every
    agent (``all``), one category's agents, or one agent. Raises
    ResourceNotFound (404), runs.InvalidRequest (400 invalid), VersionLimit
    (400), the usual run-start refusals (409, 429, 403 allowance_exceeded)
    and InsufficientAllowance (403) when the allowance left is below the
    re-run's upper-bound cost."""
    from apps.agent.runtime.swarm import plan

    swarm = get_swarm(user, website, swarm_id)
    scope_key = {"category": category, "task": task_id}.get(scope) or ""
    try:
        task_ids = plan.scope_task_ids(swarm.tasks or [], scope, scope_key)
    except ValueError:
        what = {"category": "category", "task": "agent"}.get(scope, "scope")
        raise runs.InvalidRequest(f"This research has no such {what}.") from None
    if not task_ids:
        raise runs.InvalidRequest("This research has no agents in that category.")
    wanted = set(task_ids)
    specs = [t for t in swarm.tasks or [] if t.get("id") in wanted]
    return runs.start_rerun(
        user=user, chat=swarm.chat, swarm=swarm, scope=scope, scope_key=scope_key,
        task_ids=task_ids, text=rerun_text(swarm, scope, scope_key, task_ids),
        idempotency_key=idempotency_key,
        need_usd=plan.estimate_cost(specs, with_report=False),
    )
