"""Part and task events of one swarm execution (design section 7f).

The swarm part is added once, whole: every category, every task queued
at rev 1 (``add_swarm_part``). From then on a task changes only through
its own events (task.started, task.progress, task.finished), which the
emitter applies to the persisted parts like every part event, so a
reload or a resumed stream rebuilds the tree exactly. Each task event
takes the task's next rev under its reporter's lock, so per task the
revs increase in the order the events go out. Once task.finished went
out the reporter is closed: whatever a late thread still reports is
dropped, and no event follows a task's task.finished.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime

from django.utils.dateparse import parse_datetime

from apps.agent.constants import TERMINAL_TASK_STATUSES, EventType, PartType, SwarmStatus
from apps.agent.runtime.events import now_iso
from apps.agent.runtime.parts import strip_nul
from apps.agent.tools.shaping import clip

STEP_CHARS = 80
RESULT_CHARS = 140
TARGET_CHARS = 300
# Task.detail, serialized (contract 5c).
DETAIL_MAX_CHARS = 6144
_CLIP_FLOOR = 40


def planning_part_fields() -> dict:
    """A swarm part's fields while its plan streams (contract 4b)."""
    return {
        "swarm_id": None,
        "version": None,
        "title": "",
        "scope": "all",
        "scope_label": "",
        "status": SwarmStatus.PLANNING.value,
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "categories": [],
        "tasks": [],
        "table_version": None,
        "error": None,
        "limits": None,
    }


def duration_ms_since(started_at: str | None, now: datetime | None = None) -> int:
    """Milliseconds from an ISO ``started_at`` to ``now``; 0 without a start."""
    started = parse_datetime(started_at) if isinstance(started_at, str) else None
    if started is None:
        return 0
    from django.utils import timezone

    return max(0, int(((now or timezone.now()) - started).total_seconds() * 1000))


def _size(value) -> int:
    return len(json.dumps(value))


def _lists(value, out: list) -> list:
    if isinstance(value, dict):
        for item in value.values():
            _lists(item, out)
    elif isinstance(value, list):
        if value:
            out.append(value)
        for item in value:
            _lists(item, out)
    return out


def _strings(value, out: list) -> list:
    """(container, key) of every string inside ``value``."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                out.append((value, key))
            else:
                _strings(item, out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                out.append((value, index))
            else:
                _strings(item, out)
    return out


def fit_detail(detail: dict | None, limit: int = DETAIL_MAX_CHARS) -> dict | None:
    """``detail`` within ``limit`` serialized characters: items are taken
    off the end of its largest lists first, then its longest strings are
    clipped. The workers clip their fields already; this is the bound."""
    if detail is None or _size(detail) <= limit:
        return detail
    detail = copy.deepcopy(detail)
    while _size(detail) > limit:
        lists = _lists(detail, [])
        if not lists:
            break
        max(lists, key=_size).pop()
    while _size(detail) > limit:
        strings = _strings(detail, [])
        if not strings:
            break
        container, key = max(strings, key=lambda ref: len(ref[0][ref[1]]))
        text = container[key]
        if len(text) <= _CLIP_FLOOR:
            break
        container[key] = clip(text, max(_CLIP_FLOOR, len(text) // 2))
    return detail


class TaskReporter:
    """The events of one task. Thread-safe; closed by ``finished``."""

    def __init__(self, emitter, part_id: str, task: dict, lock: threading.Lock):
        self.emitter = emitter
        self.part_id = part_id
        self.task_id = task["id"]
        self._lock = lock
        # The task as the part holds it, kept up to date with every patch.
        self._task = copy.deepcopy(task)
        self._sent_attempt = int(task.get("attempt") or 0)
        self._attempt = self._sent_attempt
        self._started: float | None = None
        self.closed = False

    @property
    def task(self) -> dict:
        """The task with every event applied (a copy)."""
        with self._lock:
            return copy.deepcopy(self._task)

    @property
    def started_monotonic(self) -> float | None:
        return self._started

    def _emit(self, kind: str, patch: dict) -> None:
        rev = int(self._task.get("rev") or 0) + 1
        patch = strip_nul(patch)
        self.emitter.emit(kind, part_id=self.part_id, task_id=self.task_id, rev=rev,
                          patch=patch)
        self._task.update(copy.deepcopy(patch))
        self._task["rev"] = rev

    def started(self, step: str) -> None:
        """task.started: running, its first step, attempt 1."""
        with self._lock:
            if self.closed or self._started is not None:
                return
            self._started = time.monotonic()
            self._attempt = self._sent_attempt = max(1, self._attempt)
            self._emit(EventType.TASK_STARTED, {
                "status": "running",
                "started_at": now_iso(),
                "step": clip(step, STEP_CHARS),
                "attempt": self._attempt,
            })

    def step(self, text: str, *, target: str | None = None) -> None:
        """task.progress with a new step line (and a new target). A repeat
        of the line shown already sends nothing."""
        with self._lock:
            if self.closed:
                return
            patch = {}
            text = clip(text, STEP_CHARS)
            if text != self._task.get("step"):
                patch["step"] = text
            if target is not None and clip(target, TARGET_CHARS) != self._task.get("target"):
                patch["target"] = clip(target, TARGET_CHARS)
            if self._attempt != self._sent_attempt:
                patch["attempt"] = self._sent_attempt = self._attempt
            if patch:
                self._emit(EventType.TASK_PROGRESS, patch)

    def retry(self) -> None:
        """The primary action is tried again: the next event carries the
        new attempt number."""
        with self._lock:
            if not self.closed:
                self._attempt += 1

    def record_capture(self, kind: str, capture_id: str, capture: dict) -> bool:
        """research.recorded for a capture this task took (dropped once the
        task finished, so a late capture never follows task.finished).
        True when it was recorded."""
        with self._lock:
            if self.closed:
                return False
            self.emitter.record_capture(kind, capture_id, capture)
            return True

    def finished(self, status: str, *, result: str = "", tone: str | None = None,
                 detail: dict | None = None, error: dict | None = None,
                 pane: dict | None = None, target: str | None = None) -> bool:
        """task.finished, once. False when the task had finished already."""
        with self._lock:
            if self.closed:
                return False
            self.closed = True
            elapsed = time.monotonic() - self._started if self._started is not None else 0
            patch = {
                "status": status,
                "step": "",
                "ended_at": now_iso(),
                "duration_ms": int(elapsed * 1000),
                "result": clip(result, RESULT_CHARS),
                "tone": tone,
                "pane": pane,
                "error": error,
                "detail": fit_detail(detail),
            }
            if target is not None:
                patch["target"] = clip(target, TARGET_CHARS)
            if self._attempt != self._sent_attempt:
                patch["attempt"] = self._sent_attempt = self._attempt
            self._emit(EventType.TASK_FINISHED, patch)
            return True


def add_swarm_part(emitter, *, part_id: str | None, request: int, swarm, version,
                   categories: list[dict], tasks: list[dict], scope: str, scope_label: str,
                   limits: dict | None, started_at: str) -> str:
    """The full swarm part, at the start of an execution (contract 4b, the
    second example). With ``part_id`` it replaces that part (the planning
    part of the tool call); otherwise it is a new part. Returns its id."""
    part_id = part_id or emitter.new_part_id()
    emitter.emit(EventType.PART_ADDED, part={
        "id": part_id,
        "type": PartType.SWARM,
        "request": request,
        "swarm_id": str(swarm.id),
        "version": version.version,
        "title": swarm.title,
        "scope": scope,
        "scope_label": scope_label,
        "status": SwarmStatus.RUNNING.value,
        "started_at": started_at,
        "ended_at": None,
        "duration_ms": None,
        "table_version": None,
        "error": None,
        "limits": limits,
        "categories": categories,
        "tasks": tasks,
    })
    return part_id


def close_swarm_part(emitter, part_id: str, *, status: str, ended_at: str,
                     duration_ms: int) -> None:
    """part.updated with the execution's end (never ``tasks``)."""
    emitter.update_part(part_id, status=status, ended_at=ended_at, duration_ms=duration_ms)


def close_open_tasks(emitter, part: dict, *, status: str, error: dict) -> None:
    """task.finished with ``status`` for every task of ``part`` that has not
    finished (a run that ends with its swarm part still open)."""
    now = now_iso()
    for task in part.get("tasks") or []:
        if task.get("status") in TERMINAL_TASK_STATUSES:
            continue
        emitter.emit(EventType.TASK_FINISHED, part_id=part["id"], task_id=task["id"],
                     rev=int(task.get("rev") or 0) + 1, patch={
                         "status": status,
                         "step": "",
                         "ended_at": now,
                         "duration_ms": duration_ms_since(task.get("started_at")),
                         "error": error,
                     })
