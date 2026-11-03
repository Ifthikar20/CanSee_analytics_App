"""The parts reducer: how part events build AgentMessage.parts.

The frontend applies the same rules to the same events
(src/lib/agent/reducer.js). The runtime applies them to the parts it
persists, so replaying a run's events over the parts the message started
with always reproduces what is stored.
"""
from __future__ import annotations

import copy

from apps.agent.constants import TASK_EVENT_TYPES, EventType, PartType

# Events that change parts. Everything else (lifecycle, artifact, browser
# and research.recorded events) leaves them alone.
PART_EVENT_TYPES = frozenset({
    EventType.PART_ADDED,
    EventType.PART_DELTA,
    EventType.PART_UPDATED,
    EventType.TURN_RESET,
    *TASK_EVENT_TYPES,
})


def strip_nul(value):
    """``value`` with every NUL removed from its strings (JSONB rejects them)."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [strip_nul(v) for v in value]
    return value


class PartsState:
    """An ordered list of parts that part events mutate in place."""

    def __init__(self, parts: list[dict] | None = None):
        self.parts: list[dict] = copy.deepcopy(list(parts or []))

    def get(self, part_id: str) -> dict | None:
        for part in self.parts:
            if part.get("id") == part_id:
                return part
        return None

    def apply(self, event: dict) -> None:
        kind = event.get("type")
        if kind == EventType.PART_ADDED:
            part = copy.deepcopy(event["part"])
            existing = self.get(part.get("id"))
            if existing is not None:
                existing.clear()
                existing.update(part)
            else:
                self.parts.append(part)
        elif kind == EventType.PART_DELTA:
            part = self.get(event.get("part_id"))
            if part is not None:
                field = event.get("field") or "text"
                part[field] = (part.get(field) or "") + (event.get("append") or "")
        elif kind == EventType.PART_UPDATED:
            part = self.get(event.get("part_id"))
            if part is not None:
                part.update(copy.deepcopy(event.get("patch") or {}))
        elif kind == EventType.TURN_RESET:
            request = event.get("request")
            self.parts = [p for p in self.parts if p.get("request") != request]
        elif kind in TASK_EVENT_TYPES:
            self._apply_task(event)

    def _apply_task(self, event: dict) -> None:
        """task.started / task.progress / task.finished (swarm contract 4c):
        the patch is merged into the task with ``task_id`` of the swarm part
        ``part_id`` when its ``rev`` is newer than the task's; a missing
        part or task, or an old rev, leaves the parts unchanged."""
        part = self.get(event.get("part_id"))
        if part is None or part.get("type") != PartType.SWARM:
            return
        task_id = event.get("task_id")
        task = next((t for t in part.get("tasks") or [] if t.get("id") == task_id), None)
        rev = event.get("rev")
        if task is None or not isinstance(rev, int) or rev <= int(task.get("rev") or 0):
            return
        patch = {k: v for k, v in (event.get("patch") or {}).items() if k not in ("id", "rev")}
        task.update(copy.deepcopy(patch))
        task["rev"] = rev

    def snapshot(self) -> list[dict]:
        return copy.deepcopy(self.parts)


def replay(events: list[dict], parts: list[dict] | None = None) -> list[dict]:
    """The parts that ``events`` produce from ``parts`` (default: none)."""
    state = PartsState(parts)
    for event in events:
        if event.get("type") in PART_EVENT_TYPES:
            state.apply(event)
    return state.snapshot()
