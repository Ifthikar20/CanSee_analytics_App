"""What the tasks of one swarm execution share (design section 7c).

A ``SwarmRun`` per execution: the plan, the results (this version's and
the ones carried from earlier versions), the Google captures, the page
fetches (one per URL), the per-prompt engine scans and the analysis
usage. Tasks run on their own threads in thread mode, so everything they
write goes through ``lock``; usage is queued for the run's own thread,
which is the only one that writes the run row.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from apps.agent.constants import WorkerType
from apps.agent.llm.usage import UsageTotals
from apps.agent.runtime.swarm import limits

logger = logging.getLogger("apps")


class TaskStopped(Exception):
    """Raised inside a worker when its task was stopped, timed out or
    reached the research's time limit: the task ends without output."""


# An analysis that never started although its task was not stopped (no
# analysis slot came free, or the task had no time left).
ANALYSIS_NOT_STARTED = {
    "code": "timeout",
    "message": "The analysis did not start before this agent's time ran out.",
}


@dataclass
class WorkerOutput:
    status: str                    # done | failed | skipped
    result: str
    tone: str | None = None
    detail: dict | None = None
    error: dict | None = None      # {code, message}
    pane: dict | None = None
    target: str | None = None


def skipped(code: str, message: str, *, target: str | None = None) -> WorkerOutput:
    """The task did not run its action, for a stated reason."""
    return WorkerOutput(status="skipped", result="", tone="muted",
                        error={"code": code, "message": message}, target=target)


def failed(code: str, message: str, *, target: str | None = None,
           detail: dict | None = None) -> WorkerOutput:
    """The task's action ran and failed (``message`` is public text)."""
    return WorkerOutput(status="failed", result="", tone="bad",
                        error={"code": code, "message": message}, target=target, detail=detail)


def iso(value) -> str | None:
    """A datetime as the events write timestamps (UTC, milliseconds, Z)."""
    if value is None:
        return None
    from datetime import UTC

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plural(count: int, word: str, words: str | None = None) -> str:
    return f"{count} {word if count == 1 else (words or word + 's')}"


@dataclass
class PageSlot:
    """One URL fetched in this execution: the first task that wants it
    fetches it, the others wait for ``ready`` and reuse what it got."""

    owner: str
    owner_label: str
    ready: threading.Event = field(default_factory=threading.Event)
    capture: dict | None = None      # the PageCapture, without its reader
    text: str = ""                   # the reader text, clipped
    error: dict | None = None
    pane: dict | None = None
    # The capture is in the research store (a pane tab can open it).
    recorded: bool = False


@dataclass
class TaskJob:
    """One running task, as its worker sees it."""

    spec: dict
    run: SwarmRun
    reporter: object
    started: float
    # The primary resource the task holds (limits), released once.
    resource: str | None = None
    # The task's own time limit (monotonic).
    deadline: float = math.inf
    _released: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def task_id(self) -> str:
        return self.spec["id"]

    @property
    def inputs(self) -> dict:
        return self.spec.get("inputs") or {}

    def stopped(self) -> bool:
        """Stop was pressed, the research reached its time limit, or the
        task was finished without this thread (it timed out)."""
        return (bool(getattr(self.reporter, "closed", False)) or self.run.cancelled()
                or time.monotonic() >= self.run.deadline)

    def check(self) -> None:
        if self.stopped():
            raise TaskStopped()

    def remaining(self) -> float:
        return self.run.remaining(self.deadline)

    def release_primary(self) -> None:
        """Give back the primary resource as soon as the primary call
        returned (also called when the thread ends; once)."""
        with self._lock:
            if self._released or self.resource is None or self.run.inline:
                self._released = True
                return
            self._released = True
        limits.release(self.resource)


class _ResearchView:
    """The run's emitter as the research helpers read it from a task's
    thread: its research store copied under the emitter's lock, so a
    capture another task records meanwhile cannot change it under them."""

    def __init__(self, emitter):
        copier = getattr(emitter, "research_copy", None)
        self.research = copier() if copier else copy.deepcopy(emitter.research)


class SwarmRun:
    def __init__(self, ctx, swarm, version, *, deadline: float, carried: dict[str, dict]):
        self.ctx = ctx
        self.swarm = swarm
        self.version = version
        self.website = ctx.website
        self.plan: dict = swarm.plan or {}
        self.prompts: dict[str, dict] = {p["key"]: p for p in self.plan.get("prompts") or []}
        self.specs: dict[str, dict] = {t["id"]: t for t in swarm.tasks or []}
        self.deadline = deadline
        self.inline = bool(ctx.inline)
        self.lock = threading.RLock()
        # task_id -> final Task of this execution.
        self.results: dict[str, dict] = {}
        # task_id -> {"version", "message_id", "task"} from earlier versions.
        self.carried = carried
        # prompt key -> {page: SerpCapture} captured (or reused) now.
        self.captures: dict[str, dict[int, dict]] = {}
        self.pages: dict[str, PageSlot] = {}
        # task_id -> {"seo": SeoFacts, "text": reader text} of a page read now.
        self.page_texts: dict[str, dict] = {}
        self.engine_scans: dict[str, object] = {}
        self.usage: queue.SimpleQueue = queue.SimpleQueue()
        self.cost_usd = Decimal("0")
        self.worker_requests = 0
        # Category notes added while the tasks run (contract 5b + "category").
        self.notes: list[dict] = []
        self._brand_prompts: dict[str, object] = {}
        self._carried_serp: dict[str, dict] = {}
        self._competitors: dict[str, str] | None = None

    # ── State ────────────────────────────────────────────────────────

    def cancelled(self) -> bool:
        return self.ctx.cancelled()

    def remaining(self, task_deadline: float = math.inf) -> float:
        """Seconds left before the task's own limit or the research's,
        whichever comes first."""
        return max(0.0, min(task_deadline, self.deadline) - time.monotonic())

    def set_result(self, task_id: str, task: dict) -> None:
        with self.lock:
            self.results[task_id] = task

    def result(self, task_id: str) -> dict | None:
        """The newest Task for ``task_id``: this execution's, else the one
        carried from an earlier version."""
        with self.lock:
            if task_id in self.results:
                return self.results[task_id]
            carried = self.carried.get(task_id)
        return carried["task"] if carried else None

    def done(self, task_id: str) -> dict | None:
        """The newest Task for ``task_id`` when it is ``done``."""
        task = self.result(task_id)
        return task if task and task.get("status") == "done" else None

    def ran_now(self, task_id: str) -> bool:
        with self.lock:
            return task_id in self.results

    def prompt_specs(self, prompt_key: str, worker: str | None = None) -> list[dict]:
        """The swarm's task specs of one prompt (task order)."""
        return [s for s in self.specs.values()
                if s.get("subcategory") == prompt_key
                and (worker is None or s.get("worker") == worker)]

    def add_note(self, category: str, level: str, code: str, text: str) -> None:
        """A run-time category note, once per category and code."""
        with self.lock:
            if any(n["category"] == category and n["code"] == code for n in self.notes):
                return
            self.notes.append({"category": category, "level": level, "code": code,
                               "text": text})

    # ── Project data ─────────────────────────────────────────────────

    def brand_prompt(self, prompt_key: str):
        """The plan prompt's BrandPrompt (with its Prompt), or None when it
        is no longer tracked."""
        from apps.prompt_library.models import BrandPrompt

        with self.lock:
            if prompt_key in self._brand_prompts:
                return self._brand_prompts[prompt_key]
        prompt = self.prompts.get(prompt_key) or {}
        found = None
        if prompt.get("brand_prompt_id"):
            found = (BrandPrompt.objects.select_related("prompt")
                     .filter(id=prompt["brand_prompt_id"], website=self.website).first())
        with self.lock:
            return self._brand_prompts.setdefault(prompt_key, found)

    def competitors(self) -> dict[str, str]:
        """The project's competitors: registrable domain -> name."""
        from apps.agent.tools.research import _competitor_apexes

        with self.lock:
            if self._competitors is None:
                self._competitors = _competitor_apexes(self.website)
            return self._competitors

    def tool_ctx(self):
        """The run's ToolContext for the research helpers (_recent_captures,
        _may_fetch), reading a copy of the research store."""
        return dataclasses.replace(self.ctx, emitter=_ResearchView(self.ctx.emitter))

    # ── Google captures ──────────────────────────────────────────────

    def set_capture(self, prompt_key: str, page: int, capture: dict) -> None:
        with self.lock:
            self.captures.setdefault(prompt_key, {})[int(page)] = capture

    def serp_captures(self, prompt_key: str) -> list[dict]:
        """The prompt's Google captures in page order: the pages captured in
        this execution, and for Google tasks that did not run in it, the
        capture their carried result opens (from that message's research)."""
        out = []
        for spec in self.prompt_specs(prompt_key, WorkerType.SERP_PAGE):
            task = self.done(spec["id"])
            if task is None:
                continue
            page = int((spec.get("inputs") or {}).get("page") or 1)
            if self.ran_now(spec["id"]):
                with self.lock:
                    capture = (self.captures.get(prompt_key) or {}).get(page)
            else:
                capture = self._carried_capture(spec["id"], task)
            if capture is not None:
                out.append(capture)
        return out

    def _carried_capture(self, task_id: str, task: dict) -> dict | None:
        from apps.agent.models import AgentMessage

        tab_id = str((task.get("pane") or {}).get("tab_id") or "")
        carried = self.carried.get(task_id) or {}
        message_id = carried.get("message_id")
        if not tab_id.startswith("serp:") or not message_id:
            return None
        with self.lock:
            store = self._carried_serp.get(message_id)
        if store is None:
            research = (AgentMessage.objects.filter(id=message_id)
                        .values_list("research", flat=True).first()) or {}
            store = research.get("serp") or {}
            with self.lock:
                self._carried_serp[message_id] = store
        return store.get(tab_id.split(":", 1)[1])

    # ── Engines ──────────────────────────────────────────────────────

    def engine_scan(self, prompt_key: str):
        """The prompt's EngineScan (one PromptCrawlRun and one audit for
        every engine asked for it), created on first use. None when the
        prompt is no longer tracked."""
        from apps.prompt_library.services.prompt_crawler import EngineScan

        with self.lock:
            scan = self.engine_scans.get(prompt_key)
            if scan is not None:
                return scan
        brand_prompt = self.brand_prompt(prompt_key)
        if brand_prompt is None:
            return None
        with self.lock:
            scan = self.engine_scans.get(prompt_key)
            if scan is None:
                scan = EngineScan(self.website, brand_prompt.prompt, acting_user=self.ctx.user)
                self.engine_scans[prompt_key] = scan
            return scan

    def finish_engine_scans(self, *, stopped: bool) -> None:
        with self.lock:
            scans = list(self.engine_scans.values())
        for scan in scans:
            try:
                scan.finish(stopped=stopped)
            except Exception:
                logger.exception("agent swarm: an engine scan could not be finished")

    # ── Usage ────────────────────────────────────────────────────────

    def add_usage(self, totals: UsageTotals, requests: int = 1) -> None:
        """An analysis request's usage. It counts toward the run's cost but
        not its request cap; in thread mode the run's thread adds it."""
        with self.lock:
            self.cost_usd += totals.cost_usd
            self.worker_requests += requests
        zeroed = dataclasses.replace(totals, requests=0)
        if self.inline:
            if self.ctx.add_usage is not None:
                self.ctx.add_usage(zeroed)
        else:
            self.usage.put(zeroed)

    def drain_usage(self) -> None:
        """Hand the queued usage to the run (the run's own thread only)."""
        total: UsageTotals | None = None
        while True:
            try:
                item = self.usage.get_nowait()
            except queue.Empty:
                break
            total = total or UsageTotals()
            total.add(item)
        if total is not None and self.ctx.add_usage is not None:
            self.ctx.add_usage(total)
