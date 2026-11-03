"""research_swarm: the research agents' planning tool (swarm_contract.md 3).

One tool call runs a whole research: the plan is validated and resolved
(prompts given as text are tracked first), expanded into 1 to 30 tasks
(runtime.swarm.plan), checked against the cap and the user's AI
allowance, and executed inside the call (runtime.swarm.executor). The
consolidated table comes back as the one tool_result, so the wire keeps
exactly one tool_use / tool_result pair however many agents ran.

A refusal first closes the planning part the stream translator added
(status "error", error {code, message}), then returns the same message to
the model as an ``is_error`` result. ``rerun_of`` runs an earlier swarm of
this chat again as its next version, with the same plan and fresh data.
"""
from __future__ import annotations

import json
import logging
from typing import NoReturn

from django.db import transaction
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    ACTIVE_VERSION_STATUSES,
    MAX_SWARM_VERSIONS,
    SWARM_TITLE_MAX_LENGTH,
    SwarmScope,
    SwarmStatus,
    TaskStatus,
    VersionStatus,
)
from apps.agent.llm.gate import CancelRequested, GateStop
from apps.agent.runtime.events import now_iso
from apps.agent.runtime.swarm import plan
from apps.agent.tools import data, research
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.schemas import TrackPrompt, TrackPromptsInput
from apps.agent.tools.shaping import clip, compact

logger = logging.getLogger("apps")

# Contract 3g: the model and the user see the same text.
SWARM_DISABLED = "The research agents are turned off on this server."
NOTHING_TO_MEASURE = ("Neither Google results nor any AI engine is configured, so the research "
                      "agents have nothing to measure.")
NO_AGENTS = ("This plan has no agents to run: none of the engines it asks for is configured "
             "or selected for these prompts. Ask the configured engines, or turn content_gaps "
             "on.")
ALLOWANCE_EXCEEDED = "You have used your monthly AI allowance."
SWARM_BUSY = "That research is running now; wait for it to finish."
VERSION_LIMIT = (f"This research already has {MAX_SWARM_VERSIONS} versions; start a new "
                 "research.")
INTERNAL_ERROR = {"code": "internal_error",
                  "message": "The research agents stopped with an internal error."}


class ResearchStopped(CancelRequested):
    """Stop ended a research after the table of what finished was made. The
    run still ends cancelled, but the call's one tool_result is that
    table's result (status "cancelled", with the swarm_id a later rerun_of
    needs), not "Stopped before this ran.", which would be untrue."""

    def __init__(self, tool_result: ToolResult):
        super().__init__()
        self.tool_result = tool_result


def _refuse(ctx: ToolContext, code: str, message: str) -> NoReturn:
    """Close the planning part with the refusal, then refuse the call."""
    if ctx.part_id:
        ctx.emitter.update_part(ctx.part_id, status=SwarmStatus.ERROR.value,
                                error={"code": code, "message": message}, ended_at=now_iso())
    raise ToolError(message, summary=message)


# ── Checks ────────────────────────────────────────────────────────────────


def _check_spent(ctx: ToolContext) -> None:
    from core.llm import allowance_denial

    if allowance_denial(ctx.user):
        _refuse(ctx, "allowance_exceeded", ALLOWANCE_EXCEEDED)


def _check_affordable(ctx: ToolContext, specs: list[dict]) -> None:
    """The plan's upper-bound AI cost, report included, against what is
    left of the monthly allowance (an unlimited cap passes)."""
    from apps.agent.services.swarms import allowance_shortfall

    shortfall = allowance_shortfall(ctx.user, plan.estimate_cost(specs, with_report=True))
    if shortfall is not None:
        _refuse(ctx, "insufficient_allowance", (
            f"Not enough AI allowance: this research needs up to ${shortfall['need_usd']:.2f} "
            f"and ${max(shortfall['left_usd'], 0):.2f} of the ${shortfall['cap_usd']:.2f} "
            "monthly allowance is left. Use fewer prompts or engines."
        ))


def _check_measurable(ctx: ToolContext, engines: list[dict], serp_configured: bool) -> None:
    """Without Google results and without any engine there is nothing to
    measure (reading the brand's own page alone compares it with nothing)."""
    if not serp_configured and not any(e["configured"] for e in engines):
        _refuse(ctx, "nothing_to_measure", NOTHING_TO_MEASURE)


def _check_size(ctx: ToolContext, expansion, args, engines: list[dict],
                serp_configured: bool) -> None:
    """1 to AGENT_SWARM_MAX_TASKS tasks, or a refusal that says how to fit."""
    max_tasks = agent_conf().swarm_max_tasks
    count = len(expansion.tasks)
    if 0 < count <= max_tasks:
        return
    configured = [e["key"] for e in engines if e["configured"]]
    if count == 0:
        _refuse(ctx, "nothing_to_measure", NO_AGENTS)
    wanted = set(args.engines) if args.engines else None
    asked = len([k for k in configured if wanted is None or k in wanted])
    fits = plan.max_prompts_for(
        engines_per_prompt=asked, google_pages=args.google_pages,
        competitor_pages=args.competitor_pages, content_gaps=args.content_gaps,
        citations=args.citations, serp_configured=serp_configured, max_tasks=max_tasks,
    )
    _refuse(ctx, "too_many_agents", (
        f"This plan has {count} agents; the limit is {max_tasks}. With {asked} engines "
        f"configured, use at most {fits} prompts, or fewer Google or competitor pages."
    ))


# ── Prompts ───────────────────────────────────────────────────────────────


def _resolved(bp, key: str, *, tracked_now: bool) -> plan.ResolvedPrompt:
    return plan.ResolvedPrompt(
        key=key,
        prompt_id=str(bp.prompt_id),
        brand_prompt_id=str(bp.id),
        text=" ".join(bp.prompt.text.split()),
        location=(bp.location or "").lower(),
        model_variants=tuple(str(v) for v in bp.model_variants or ()),
        tracked_now=tracked_now,
    )


def _resolve_prompts(ctx: ToolContext, items) -> list[plan.ResolvedPrompt]:
    """The plan's prompts as tracked prompts, in input order, one per
    tracked prompt (duplicates merged). New texts are tracked in one
    track_prompts call, which refuses prompts the user has not seen."""
    texts = [item for item in items if item.text is not None]
    tracked: dict[int, tuple[str, bool]] = {}
    if texts:
        try:
            result = research.track_prompts(ctx, TrackPromptsInput(prompts=[
                TrackPrompt(text=item.text, location=item.location) for item in texts
            ]))
        except ToolError as exc:
            _refuse(ctx, "needs_confirmation", exc.message)
        rows = json.loads(result.content)["prompts"]
        for item, row in zip(texts, rows, strict=True):
            tracked[id(item)] = (row["prompt_id"], not row["already_tracked"])
    out: list[plan.ResolvedPrompt] = []
    seen: set[str] = set()
    for item in items:
        if item.text is not None:
            prompt_id, tracked_now = tracked[id(item)]
        else:
            prompt_id, tracked_now = str(item.prompt_id), False
        if prompt_id in seen:
            continue
        try:
            bp = data.tracked_prompt(ctx.website, prompt_id)
        except ToolError as exc:
            _refuse(ctx, "prompt_not_found", exc.message)
        seen.add(prompt_id)
        out.append(_resolved(bp, f"p{len(out) + 1}", tracked_now=tracked_now))
    return out


def _options(args, website) -> plan.PlanOptions:
    title = args.title or f"Research {website.name}"
    return plan.PlanOptions(
        title=clip(" ".join(title.split()), SWARM_TITLE_MAX_LENGTH),
        google_pages=args.google_pages,
        engines=tuple(dict.fromkeys(args.engines)) if args.engines else None,
        competitor_pages=args.competitor_pages,
        # The expansion keeps the entries that name a domain and notes the rest.
        competitors=tuple(args.competitors),
        content_gaps=args.content_gaps,
        citations=args.citations,
    )


# ── Rows ──────────────────────────────────────────────────────────────────


def _rerun_target(ctx: ToolContext, swarm_id):
    from apps.agent.models import AgentSwarm, AgentSwarmVersion

    swarm = AgentSwarm.objects.filter(id=swarm_id, chat=ctx.chat).first()
    if swarm is None:
        _refuse(ctx, "swarm_not_found", f"No research with swarm_id {swarm_id} in this chat.")
    versions = AgentSwarmVersion.objects.filter(swarm=swarm)
    # Busy means a version whose run still runs: a version row its ended
    # run left behind must not refuse every later re-run.
    if versions.filter(status__in=ACTIVE_VERSION_STATUSES,
                       run__status__in=ACTIVE_RUN_STATUSES).exists():
        _refuse(ctx, "swarm_busy", SWARM_BUSY)
    if versions.count() >= MAX_SWARM_VERSIONS:
        _refuse(ctx, "version_limit", VERSION_LIMIT)
    return swarm


def _create_swarm(ctx: ToolContext, expansion):
    from apps.agent.models import AgentSwarm

    return AgentSwarm.objects.create(
        website=ctx.website, chat=ctx.chat, user=ctx.user,
        message_id=ctx.run.assistant_message_id, run=ctx.run,
        title=expansion.plan["title"], plan=expansion.plan,
        categories=expansion.categories, tasks=expansion.tasks,
    )


def _new_version(ctx: ToolContext, swarm, task_ids: list[str]):
    """The swarm's next version, run by this tool call (every task)."""
    from apps.agent.models import AgentSwarm, AgentSwarmVersion

    with transaction.atomic():
        AgentSwarm.objects.select_for_update().filter(id=swarm.id).first()
        number = (AgentSwarmVersion.objects.filter(swarm=swarm).order_by("-version")
                  .values_list("version", flat=True).first() or 0) + 1
        return AgentSwarmVersion.objects.create(
            swarm=swarm, version=number, scope=SwarmScope.ALL, scope_key="",
            task_ids=list(task_ids), status=VersionStatus.RUNNING, run=ctx.run,
            message_id=ctx.run.assistant_message_id,
        )


def _fail_execution(ctx: ToolContext, version) -> None:
    """The execution itself raised (a programming or database error, never a
    task failure): the version ends in error and the swarm part closes, so
    nothing shows as running. The registry reports the error to the model."""
    from apps.agent.models import AgentSwarmVersion
    from apps.agent.runtime.swarm.reporter import close_open_tasks

    try:
        AgentSwarmVersion.objects.filter(
            id=version.id, status__in=ACTIVE_VERSION_STATUSES,
        ).update(status=VersionStatus.ERROR, error_code=INTERNAL_ERROR["code"],
                 error_message=INTERNAL_ERROR["message"], finished_at=timezone.now())
        part = ctx.emitter.part(ctx.part_id) if ctx.part_id else None
        if part and part.get("status") in (SwarmStatus.PLANNING, SwarmStatus.RUNNING):
            close_open_tasks(ctx.emitter, part, status=TaskStatus.STOPPED.value,
                             error=INTERNAL_ERROR)
            ctx.emitter.update_part(ctx.part_id, status=SwarmStatus.ERROR.value,
                                    error=INTERNAL_ERROR, ended_at=now_iso())
    except Exception:
        logger.exception("agent swarm: could not close a failed execution (run %s)", ctx.run.id)


# ── The tool ──────────────────────────────────────────────────────────────


def research_swarm(ctx: ToolContext, args) -> ToolResult:
    from apps.agent.runtime.swarm import executor
    from apps.agent.services import swarm_tables
    from apps.citations.services import serp_google

    if not agent_conf().swarm_enabled:
        _refuse(ctx, "swarm_disabled", SWARM_DISABLED)
    _check_spent(ctx)
    if args.rerun_of is not None:
        swarm = _rerun_target(ctx, args.rerun_of)
        specs = [t for t in swarm.tasks or [] if isinstance(t, dict)]
        _check_affordable(ctx, specs)
    else:
        engines = research.engine_catalog()
        serp_configured = serp_google.is_configured()
        _check_measurable(ctx, engines, serp_configured)
        prompts = _resolve_prompts(ctx, args.prompts)
        expansion = plan.build_expansion(
            ctx.website, prompts, _options(args, ctx.website), engines=engines,
            serp_configured=serp_configured, now_iso=now_iso(),
        )
        _check_size(ctx, expansion, args, engines, serp_configured)
        specs = expansion.tasks
        _check_affordable(ctx, specs)
        swarm = _create_swarm(ctx, expansion)
    version = _new_version(ctx, swarm, [t["id"] for t in specs])

    try:
        outcome = executor.execute_version(ctx, swarm, version, part_id=ctx.part_id)
        finalized = swarm_tables.finalize_version(ctx, swarm, version, outcome)
    except (CancelRequested, GateStop):
        raise
    except Exception:
        _fail_execution(ctx, version)
        raise
    if outcome.cancelled:
        # The table of what finished is kept and answers the call; the run
        # ends cancelled.
        raise ResearchStopped(ToolResult.ok(finalized.tool_result, summary=finalized.summary))
    if finalized.table is None:
        return ToolResult(content=compact(finalized.tool_result), is_error=True,
                          summary=finalized.summary)
    return ToolResult.ok(finalized.tool_result, summary=finalized.summary)
