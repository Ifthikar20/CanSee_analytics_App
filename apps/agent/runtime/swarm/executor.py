"""One execution (version) of a research swarm (design section 7).

``execute_version`` runs the tasks of one AgentSwarmVersion: inside the
research_swarm tool call (version 1, or ``rerun_of``) or in a re-run
turn. It adds the full swarm part (replacing the tool call's planning
part), runs the tasks, records the Google rank checks it can stand
behind, finishes the engine scans, closes the part and saves the version
row. It never raises for a task failure, Stop or the time limit; only a
programming or database error propagates (and the caller lets it).

Thread mode: the calling thread (the run's) is the monitor. It starts a
task on its own daemon thread once the task's dependencies have finished
and a slot is free: one of AGENT_SWARM_WORKERS for this swarm, one of
the process's ``task`` slots and the task's primary resource (limits).
It times tasks out, stops them on Stop and at the research's time limit,
hands the analysis usage to the run and checkpoints the parts every few
seconds. A task that times out is finished without its thread: whatever
the thread still reports is dropped by its closed reporter, and the
resource it holds is released when its call returns.

Inline mode (tests): the tasks run one after another in task order in
the calling thread (an order that satisfies every dependency), without
threads, limits or task time limits; Stop and the research's time limit
are checked before each task.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import SwarmScope, SwarmStatus, VersionStatus, WorkerType
from apps.agent.runtime.swarm import limits, plan, reporter, workers
from apps.agent.runtime.swarm.context import SwarmRun, TaskJob, TaskStopped, WorkerOutput, iso
from apps.agent.runtime.swarm.reporter import TaskReporter

logger = logging.getLogger("apps")

# Seconds a task may run, from task.started (contract section 8).
TASK_TIMEOUT = {
    WorkerType.SERP_PAGE: 90,
    WorkerType.ENGINE_ANSWER: 150,
    WorkerType.PAGE_INSPECT: 40,
    WorkerType.CONTENT_GAP: 60,
    WorkerType.CITATIONS: 45,
}
DEFAULT_TASK_TIMEOUT = 60
POLL_SECONDS = 0.25
# A research ends at least this long before its run's own deadline, so the
# orchestrator still has time to write the report.
RUN_DEADLINE_MARGIN_SECONDS = 240

STOPPED_PENDING = {"code": "stopped", "message": "Stopped before it started"}
STOPPED_RUNNING = {"code": "stopped", "message": "Stopped"}
DEADLINE_PENDING = {"code": "deadline",
                    "message": "Not started: the research reached its time limit."}
DEADLINE_RUNNING = {"code": "deadline", "message": "Stopped at the research's time limit."}
INTERNAL_ERROR = {"code": "internal_error", "message": "This agent failed with an internal error."}
NOTHING_TO_RUN = {"code": "internal_error", "message": "This research has no agents to run."}


@dataclass
class VersionOutcome:
    status: str                      # done | partial | cancelled | error
    cancelled: bool
    part_id: str
    request: int                     # the part's request index
    results: dict[str, dict]         # task_id -> final Task (contract 5c), run in this version
    carried: dict[str, dict]         # task_id -> {"version", "message_id", "task"}
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    cost_usd: Decimal
    worker_requests: int
    rank_checks: dict[str, bool]     # prompt key -> a GoogleRankCheck was recorded now
    notes: list[dict]                # run-time category notes (contract 5b plus "category")
    # {code, message} when the execution itself failed (status "error").
    error: dict | None = field(default=None)


def _timeout(spec: dict) -> float:
    return TASK_TIMEOUT.get(spec.get("worker"), DEFAULT_TASK_TIMEOUT)


def _deadline(ctx, conf, started: float) -> float:
    """The research's time limit (monotonic): AGENT_SWARM_MAX_SECONDS, and
    never past the run's deadline minus RUN_DEADLINE_MARGIN_SECONDS."""
    deadline = started + conf.swarm_max_seconds
    run_started = (ctx.extra or {}).get("run_started")
    if isinstance(run_started, int | float):
        deadline = min(deadline,
                       run_started + conf.run_max_seconds - RUN_DEADLINE_MARGIN_SECONDS)
    return deadline


def _carried(swarm, version, running: set[str]) -> dict[str, dict]:
    """For every task of the swarm this version does not run: its newest
    result in an earlier version, with that version and its message."""
    from apps.agent.models import AgentSwarmVersion

    wanted = {t["id"] for t in swarm.tasks or []} - running
    out: dict[str, dict] = {}
    if not wanted:
        return out
    rows = (AgentSwarmVersion.objects.filter(swarm_id=swarm.id, version__lt=version.version)
            .order_by("-version").values_list("version", "message_id", "results"))
    for number, message_id, results in rows:
        for task_id, task in (results or {}).items():
            if task_id in wanted and task_id not in out and isinstance(task, dict):
                out[task_id] = {"version": number,
                                "message_id": str(message_id) if message_id else None,
                                "task": task}
        if len(out) == len(wanted):
            break
    return out


def _carried_note(scope: str, scope_label: str) -> dict:
    if scope == SwarmScope.CATEGORY:
        text = (f"Only the {scope_label} agents run in this version; the other categories keep "
                "their earlier results.")
    else:
        text = "Only this agent runs in this version; the other agents keep their earlier results."
    return {"level": "info", "code": "carried", "text": text}


def _part_categories(categories: list[dict], specs: list[dict], scope: str,
                     scope_label: str) -> list[dict]:
    """The swarm's categories and subcategories that have a task in this
    execution, with their notes (a notes-only category stays when every
    task runs), plus the ``carried`` note on a scoped re-run."""
    present: dict[str, set[str]] = {}
    for spec in specs:
        present.setdefault(spec["category"], set()).add(spec["subcategory"])
    out = []
    for category in categories or []:
        subcategories = present.get(category.get("key"))
        if subcategories:
            out.append({**category, "notes": list(category.get("notes") or []),
                        "subcategories": [s for s in category.get("subcategories") or []
                                          if s.get("key") in subcategories]})
        elif scope == SwarmScope.ALL and category.get("notes"):
            out.append({**category, "notes": list(category["notes"]), "subcategories": []})
    if scope != SwarmScope.ALL and out:
        out[0]["notes"].append(_carried_note(scope, scope_label))
    return out


def _counts(results: dict[str, dict], task_ids: list[str]) -> dict:
    statuses = Counter((results.get(t) or {}).get("status") for t in task_ids)
    return {"total": len(task_ids), "done": statuses["done"], "failed": statuses["failed"],
            "skipped": statuses["skipped"], "stopped": statuses["stopped"]}


class _Execution:
    """The tasks of one execution: their reporters, their state (pending,
    running or final) and the slots the running ones hold."""

    def __init__(self, run: SwarmRun, part_id: str, specs: list[dict], tasks: list[dict]):
        self.run = run
        self.specs = specs
        self.ids = {s["id"] for s in specs}
        self.reporters = {t["id"]: TaskReporter(run.ctx.emitter, part_id, t, threading.Lock())
                          for t in tasks}
        self.pending: list[dict] = list(specs)
        self.running: dict[str, TaskJob] = {}
        self.final: set[str] = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped_by_user = False

    # ── Finishing tasks ──────────────────────────────────────────────

    def finalize(self, task_id: str, status: str, *, result: str = "", tone: str | None = None,
                 detail: dict | None = None, error: dict | None = None,
                 pane: dict | None = None, target: str | None = None) -> bool:
        """Finish a task once: task.finished, its Task stored, its slots
        released. A no-op for a task that finished already."""
        with self.lock:
            if task_id in self.final:
                return False
            self.final.add(task_id)
            job = self.running.pop(task_id, None)
            self.pending = [s for s in self.pending if s["id"] != task_id]
            task_reporter = self.reporters[task_id]
            task_reporter.finished(status, result=result, tone=tone, detail=detail, error=error,
                                   pane=pane, target=target)
            self.run.set_result(task_id, task_reporter.task)
        if job is not None and not self.run.inline:
            limits.release("task")
        self.wake.set()
        return True

    def _finish_output(self, job: TaskJob, output: WorkerOutput) -> None:
        self.finalize(job.task_id, output.status, result=output.result, tone=output.tone,
                      detail=output.detail, error=output.error, pane=output.pane,
                      target=output.target)

    def _end_open(self, *, running: tuple[str, dict], pending: tuple[str, dict]) -> None:
        for spec in self.specs:
            with self.lock:
                if spec["id"] in self.final:
                    continue
                status, error = running if spec["id"] in self.running else pending
            self.finalize(spec["id"], status, error=dict(error))

    def _stop_all(self) -> None:
        """Stop: every task that has not finished is stopped."""
        self.stopped_by_user = True
        self._end_open(running=("stopped", STOPPED_RUNNING), pending=("stopped", STOPPED_PENDING))

    def _expire_all(self) -> None:
        """The research's time limit: running tasks fail, the rest are skipped."""
        self._end_open(running=("failed", DEADLINE_RUNNING),
                       pending=("skipped", DEADLINE_PENDING))

    def _stopped_reason(self) -> tuple[str, dict]:
        if self.run.cancelled():
            return "stopped", STOPPED_RUNNING
        if time.monotonic() >= self.run.deadline:
            return "failed", DEADLINE_RUNNING
        return "stopped", STOPPED_RUNNING

    # ── Running tasks ────────────────────────────────────────────────

    def _call(self, job: TaskJob) -> WorkerOutput | None:
        """The worker's output; None when it stopped (TaskStopped)."""
        worker = workers.WORKERS.get(job.spec.get("worker"))
        try:
            if worker is None:
                raise LookupError(f"no worker for {job.spec.get('worker')}")
            output = worker(job)
        except TaskStopped:
            return None
        except Exception:
            logger.exception("agent swarm task %s failed", job.task_id)
            return WorkerOutput(status="failed", result="", tone="bad",
                                error=dict(INTERNAL_ERROR))
        if not isinstance(output, WorkerOutput) or output.status not in ("done", "failed",
                                                                          "skipped"):
            logger.error("agent swarm task %s returned no valid output", job.task_id)
            return WorkerOutput(status="failed", result="", tone="bad",
                                error=dict(INTERNAL_ERROR))
        return output

    def _begin(self, spec: dict, resource: str | None, deadline: float) -> TaskJob:
        job = TaskJob(spec=spec, run=self.run, reporter=self.reporters[spec["id"]],
                      started=time.monotonic(), resource=resource, deadline=deadline)
        with self.lock:
            self.pending = [s for s in self.pending if s["id"] != spec["id"]]
            self.running[spec["id"]] = job
        try:
            step = workers.first_step(spec, self.run)
        except Exception:
            logger.exception("agent swarm task %s: no first step", spec["id"])
            step = "Working"
        job.reporter.started(step)
        return job

    def run_all(self) -> None:
        try:
            if self.run.inline:
                self._run_inline()
            else:
                self._run_threads()
        except BaseException:
            # A programming or database error: no task stays open.
            self._end_open(running=("failed", INTERNAL_ERROR), pending=("failed", INTERNAL_ERROR))
            raise

    def _run_inline(self) -> None:
        for spec in self.specs:
            if self.run.cancelled():
                self._stop_all()
                return
            if time.monotonic() >= self.run.deadline:
                self._expire_all()
                return
            job = self._begin(spec, None, math.inf)
            output = self._call(job)
            if output is not None:
                self._finish_output(job, output)
            elif self.run.cancelled():
                self._stop_all()
                return
            else:
                status, error = self._stopped_reason()
                self.finalize(job.task_id, status, error=dict(error))
            self.run.ctx.emitter.checkpoint()

    # ── Thread mode ──────────────────────────────────────────────────

    def _run_threads(self) -> None:
        conf = agent_conf()
        while True:
            if self.run.cancelled():
                self._stop_all()
                return
            now = time.monotonic()
            if now >= self.run.deadline:
                self._expire_all()
                return
            with self.lock:
                running = list(self.running.values())
            for job in running:
                if now >= job.deadline:
                    limit = _timeout(job.spec)
                    self.finalize(job.task_id, "failed", error={
                        "code": "timeout", "message": f"Timed out after {limit:g} seconds."})
            self._dispatch(conf)
            self.run.drain_usage()
            self.run.ctx.emitter.checkpoint()
            with self.lock:
                if not self.pending and not self.running:
                    return
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def _deps_settled(self, spec: dict) -> bool:
        """Every dependency finished in this execution, or is not part of
        it (its carried result, or nothing, is what the task sees)."""
        return all(dep in self.final or dep not in self.ids
                   for dep in spec.get("depends_on") or [])

    def _dispatch(self, conf) -> None:
        for spec in list(self.pending):
            with self.lock:
                if len(self.running) >= conf.swarm_workers:
                    return
                if not self._deps_settled(spec):
                    continue
            resource = limits.primary_resource(spec)
            if resource and not limits.try_acquire(resource):
                continue
            if not limits.try_acquire("task"):
                if resource:
                    limits.release(resource)
                return
            self._start(spec, resource)

    def _start(self, spec: dict, resource: str | None) -> None:
        job = self._begin(spec, resource, time.monotonic() + _timeout(spec))
        thread = threading.Thread(target=self._work, args=(job,), daemon=True,
                                  name=f"agent-swarm-{spec['id']}")
        try:
            thread.start()
        except RuntimeError:
            logger.exception("agent swarm task %s could not start a thread", spec["id"])
            job.release_primary()
            self.finalize(spec["id"], "failed", error=dict(INTERNAL_ERROR))

    def _work(self, job: TaskJob) -> None:
        close_old_connections()
        try:
            output = self._call(job)
            if output is not None:
                self._finish_output(job, output)
            elif not job.stopped():
                # The worker stopped although nothing stopped it: say so.
                status, error = self._stopped_reason()
                self.finalize(job.task_id, status, error=dict(error))
        finally:
            job.release_primary()
            connections.close_all()
            self.wake.set()


def _record_rank_checks(run: SwarmRun, specs: list[dict]) -> dict[str, bool]:
    """A GoogleRankCheck for each prompt whose Google pages all ran now and
    are done, when they give a definite answer: a position found, Google
    out of results, or the full search depth covered (a miss in the top 10
    or 20 says nothing about the positions further down, as for
    google_search)."""
    from apps.agent.tools.research import PAGE_SIZE, _record_rank_check

    ran = {s["id"] for s in specs}
    out: dict[str, bool] = {}
    for key, prompt in run.prompts.items():
        google = run.prompt_specs(key, WorkerType.SERP_PAGE)
        if not google or any(s["id"] not in ran for s in google):
            continue
        tasks = [run.results.get(s["id"]) or {} for s in google]
        captures = run.serp_captures(key)
        if any(t.get("status") != "done" for t in tasks) or len(captures) != len(google):
            out[key] = False
            continue
        positions = [p for c in captures for p in c.get("brand_positions") or []]
        exhausted = not captures[-1].get("results")
        definite = (positions or exhausted
                    or PAGE_SIZE * len(captures) >= settings.SEARCH_GAP_MAX_DEPTH)
        brand_prompt = run.brand_prompt(key) if definite else None
        if brand_prompt is None:
            out[key] = False
            continue
        fetched = sum(1 for t in tasks if not (t.get("detail") or {}).get("reused"))
        try:
            _record_rank_check(run.website, brand_prompt,
                               query=" ".join((prompt.get("text") or "").split()),
                               location=(prompt.get("location") or "").lower(),
                               pages=captures, fetched=fetched, exhausted=exhausted)
        except Exception:
            logger.exception("agent swarm: the Google rank check of %s was not recorded", key)
            out[key] = False
            continue
        out[key] = True
    return out


def _save(version, **fields) -> None:
    from apps.agent.models import AgentSwarmVersion

    AgentSwarmVersion.objects.filter(id=version.id).update(**fields)
    for name, value in fields.items():
        setattr(version, name, value)


def execute_version(ctx, swarm, version, *, part_id: str | None = None) -> VersionOutcome:
    """Run ``version`` of ``swarm`` (design section 7b). ``part_id`` is the
    planning part to replace; without it a new part is added."""
    conf = agent_conf()
    known = {s.get("id") for s in swarm.tasks or []}
    run_ids = [t for t in dict.fromkeys(version.task_ids or []) if t in known]
    specs = [s for s in swarm.tasks or [] if s.get("id") in set(run_ids)]
    carried = _carried(swarm, version, set(run_ids))
    started_at = timezone.now()
    started = time.monotonic()
    _save(version, status=VersionStatus.RUNNING.value, started_at=started_at)

    scope = version.scope or SwarmScope.ALL.value
    label = plan.scope_label(swarm.categories, swarm.tasks, (swarm.plan or {}).get("prompts"),
                             scope, version.scope_key)
    tasks = [plan.initial_task(s) for s in specs]
    part_id = reporter.add_swarm_part(
        ctx.emitter, part_id=part_id, request=ctx.request, swarm=swarm, version=version,
        categories=_part_categories(swarm.categories, specs, scope, label), tasks=tasks,
        scope=scope, scope_label=label,
        limits={"max_tasks": conf.swarm_max_tasks, "workers": conf.swarm_workers},
        started_at=iso(started_at),
    )
    ctx.emitter.checkpoint(force=True)
    run = SwarmRun(ctx, swarm, version, deadline=_deadline(ctx, conf, started), carried=carried)

    if not specs:
        ended_at = timezone.now()
        duration_ms = int((time.monotonic() - started) * 1000)
        ctx.emitter.update_part(part_id, status=SwarmStatus.ERROR.value, error=dict(NOTHING_TO_RUN),
                                ended_at=iso(ended_at), duration_ms=duration_ms)
        _save(version, status=VersionStatus.ERROR.value, counts=_counts({}, []),
              error_code=NOTHING_TO_RUN["code"], error_message=NOTHING_TO_RUN["message"],
              finished_at=ended_at, duration_ms=duration_ms)
        ctx.emitter.checkpoint(force=True)
        return VersionOutcome(
            status="error", cancelled=False, part_id=part_id, request=ctx.request, results={},
            carried=carried, started_at=started_at, ended_at=ended_at, duration_ms=duration_ms,
            cost_usd=Decimal("0"), worker_requests=0, rank_checks={}, notes=[],
            error=dict(NOTHING_TO_RUN),
        )

    execution = _Execution(run, part_id, specs, tasks)
    execution.run_all()
    cancelled = execution.stopped_by_user
    rank_checks = _record_rank_checks(run, specs)
    run.finish_engine_scans(stopped=cancelled)

    results = {t: run.results[t] for t in run_ids if t in run.results}
    if cancelled:
        status = VersionStatus.CANCELLED.value
    elif all((results.get(t) or {}).get("status") == "done" for t in run_ids):
        status = VersionStatus.DONE.value
    else:
        status = VersionStatus.PARTIAL.value
    ended_at = timezone.now()
    duration_ms = int((time.monotonic() - started) * 1000)
    reporter.close_swarm_part(ctx.emitter, part_id, status=status, ended_at=iso(ended_at),
                              duration_ms=duration_ms)
    run.drain_usage()
    cost_usd = run.cost_usd.quantize(Decimal("0.000001"))
    _save(version, status=status, results=results, counts=_counts(results, run_ids),
          cost_usd=cost_usd, worker_requests=run.worker_requests, finished_at=ended_at,
          duration_ms=duration_ms)
    ctx.emitter.checkpoint(force=True)
    return VersionOutcome(
        status=status, cancelled=cancelled, part_id=part_id, request=ctx.request,
        results=results, carried=carried, started_at=started_at, ended_at=ended_at,
        duration_ms=duration_ms, cost_usd=cost_usd, worker_requests=run.worker_requests,
        rank_checks=rank_checks, notes=list(run.notes),
    )
