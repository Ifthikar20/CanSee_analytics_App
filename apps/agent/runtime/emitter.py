"""One run's event writer: envelopes, parts, research and checkpoints.

Every event goes through ``emit``, which adds the envelope, appends it
to the event store (which assigns ``seq``) and applies part events to
the parts it persists, under one lock (tool workers emit from other
threads). ``checkpoint`` writes parts, parts_seq and the research store
to Postgres: at request and tool boundaries, and every few seconds while
a response streams.
"""
from __future__ import annotations

import copy
import re
import threading
import time

from apps.agent.constants import TERMINAL_EVENT_TYPES, EventType
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.parts import PART_EVENT_TYPES, PartsState, strip_nul

CHECKPOINT_SECONDS = 3.0
_PART_NUMBER = re.compile(r"^p(\d+)$")


class RunEmitter:
    def __init__(self, run, message, *, store=None):
        self.run_id = str(run.id)
        self.message_id = str(message.id)
        self.store = store or get_event_store()
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._parts = PartsState(message.parts or [])
        self._counter = max(
            (int(m.group(1)) for p in self._parts.parts
             if (m := _PART_NUMBER.match(str(p.get("id") or "")))),
            default=0,
        )
        self.research: dict = copy.deepcopy(message.research or {})
        self.last_seq = self.store.last_seq(self.run_id)
        self.persisted_seq = int(message.parts_seq or 0)
        self._last_checkpoint = time.monotonic()
        self._dirty = False
        # Set once the run's terminal event went out.
        self.ended = False

    # ── Events ───────────────────────────────────────────────────────

    def emit(self, type: str, **payload) -> dict:
        with self._lock:
            if type in PART_EVENT_TYPES:
                payload = strip_nul(payload)
            event = self.store.append(self.run_id, {
                "type": type,
                "run_id": self.run_id,
                "message_id": self.message_id,
                "ts": now_iso(),
                **payload,
            })
            if type in PART_EVENT_TYPES:
                self._parts.apply(event)
                self._dirty = True
            if type in TERMINAL_EVENT_TYPES:
                self.ended = True
            self.last_seq = event["seq"]
            return event

    def emit_before_end(self, type: str, **payload) -> dict | None:
        """``emit``, unless the terminal event already went out (readers
        stop there, so a later event would never be seen)."""
        with self._lock:
            if self.ended:
                return None
            return self.emit(type, **payload)

    # ── Parts ────────────────────────────────────────────────────────

    def new_part_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"p{self._counter}"

    def add_part(self, type: str, *, request: int, **fields) -> str:
        part_id = self.new_part_id()
        self.emit(EventType.PART_ADDED, part={"id": part_id, "type": type, "request": request,
                                               **fields})
        return part_id

    def update_part(self, part_id: str, **patch) -> None:
        if part_id and patch:
            self.emit(EventType.PART_UPDATED, part_id=part_id, patch=patch)

    def append_text(self, part_id: str, text: str, field: str = "text") -> None:
        if part_id and text:
            self.emit(EventType.PART_DELTA, part_id=part_id, field=field, append=text)

    def reset_request(self, request: int) -> None:
        self.emit(EventType.TURN_RESET, request=request)

    def part(self, part_id: str) -> dict | None:
        with self._lock:
            found = self._parts.get(part_id)
            return copy.deepcopy(found) if found else None

    def parts(self) -> list[dict]:
        with self._lock:
            return self._parts.snapshot()

    # ── Research store ───────────────────────────────────────────────

    def record_research(self, kind: str, key: str, value: dict) -> None:
        with self._lock:
            self.research.setdefault(kind, {})[key] = strip_nul(copy.deepcopy(value))
            self._dirty = True

    def record_gap(self, gap: dict) -> None:
        """Keep the newest get_prompt_gap result per prompt (reports use them)."""
        with self._lock:
            gaps = [g for g in self.research.get("gaps") or []
                    if (g.get("prompt") or {}).get("id") != (gap.get("prompt") or {}).get("id")]
            gaps.append(strip_nul(copy.deepcopy(gap)))
            self.research["gaps"] = gaps
            self._dirty = True

    # ── Persistence ──────────────────────────────────────────────────

    def snapshot(self) -> tuple[list[dict], int, dict]:
        with self._lock:
            return self._parts.snapshot(), self.last_seq, copy.deepcopy(self.research)

    def checkpoint(self, *, force: bool = False) -> bool:
        """Persist parts, parts_seq and research when due (or ``force``)."""
        if not force and time.monotonic() - self._last_checkpoint < CHECKPOINT_SECONDS:
            return False
        from apps.agent.models import AgentMessage, AgentRun

        with self._persist_lock:
            with self._lock:
                if not self._dirty and self.persisted_seq >= self.last_seq and not force:
                    return False
                parts, seq, research = self._parts.snapshot(), self.last_seq, copy.deepcopy(
                    self.research)
                self._dirty = False
            AgentMessage.objects.filter(id=self.message_id).update(
                parts=parts, parts_seq=seq, research=research,
            )
            AgentRun.objects.filter(id=self.run_id).update(last_seq=seq)
            self.persisted_seq = seq
            self._last_checkpoint = time.monotonic()
        return True
