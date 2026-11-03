"""Per-run event buffers: what SSE and long-poll clients read.

Postgres is the source of truth for a run (AgentMessage.parts and
parts_seq). These buffers only relay events to clients connected to this
process. A client that falls behind a buffer, or asks for a run whose
buffer is gone (finished long ago, or a process restart), gets a
``resync`` control event and reloads the message snapshot.

The store assigns ``seq`` (1, 2, 3...) per run. Sync readers block on a
threading.Condition; async readers register an asyncio.Event that
``append`` sets through ``loop.call_soon_threadsafe``. The EventStore
protocol stays abstract so a shared backend (Redis Streams) can replace
the in-process one when there is more than one agent process.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from apps.agent.constants import EVENT_VERSION, TERMINAL_EVENT_TYPES, EventType

BUFFER_MAX_EVENTS = 5000
# A finished run's buffer stays readable this long, then it is dropped.
RETAIN_AFTER_TERMINAL_SECONDS = 15 * 60
# A buffer that never got a terminal event (a crashed thread) is dropped
# after this long.
RETAIN_UNFINISHED_SECONDS = 6 * 60 * 60
READ_LIMIT = 500


def now_iso() -> str:
    """UTC timestamp with milliseconds, as every event carries it."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ReadResult:
    events: list[dict]
    # Set when the reader must reload the message snapshot instead.
    resync: str | None = None
    # The returned batch ends with the run's terminal event.
    terminal: bool = False


class EventStore(Protocol):
    def open(self, run_id: str) -> None: ...

    def append(self, run_id: str, event: dict) -> dict: ...

    def read(self, run_id: str, after: int, limit: int = READ_LIMIT) -> ReadResult: ...

    def wait(self, run_id: str, after: int, timeout: float) -> bool: ...

    async def wait_async(self, run_id: str, after: int, timeout: float) -> bool: ...

    def last_seq(self, run_id: str) -> int: ...

    def has(self, run_id: str) -> bool: ...


@dataclass
class _Buffer:
    maxlen: int
    created: float = field(default_factory=time.monotonic)
    events: deque = field(init=False)
    next_seq: int = 1
    terminal_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    waiters: set = field(default_factory=set)

    def __post_init__(self):
        self.events = deque(maxlen=self.maxlen)

    @property
    def last_seq(self) -> int:
        return self.next_seq - 1


class MemoryEventStore:
    """The in-process EventStore (one agent process)."""

    def __init__(self, *, maxlen: int = BUFFER_MAX_EVENTS):
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._buffers: dict[str, _Buffer] = {}

    # ── Buffers ──────────────────────────────────────────────────────

    def _get(self, run_id: str) -> _Buffer | None:
        with self._lock:
            return self._buffers.get(str(run_id))

    def _get_or_create(self, run_id: str) -> _Buffer:
        key = str(run_id)
        with self._lock:
            buf = self._buffers.get(key)
            if buf is None:
                self._prune_locked()
                buf = _Buffer(maxlen=self._maxlen)
                self._buffers[key] = buf
            return buf

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key for key, buf in self._buffers.items()
            if (buf.terminal_at is not None and now - buf.terminal_at > RETAIN_AFTER_TERMINAL_SECONDS)
            or (buf.terminal_at is None and now - buf.created > RETAIN_UNFINISHED_SECONDS)
        ]
        for key in stale:
            del self._buffers[key]

    def open(self, run_id: str) -> None:
        """Create the run's buffer so readers can attach before the first event."""
        self._get_or_create(run_id)

    def has(self, run_id: str) -> bool:
        return self._get(run_id) is not None

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._buffers.pop(str(run_id), None)

    # ── Writing ──────────────────────────────────────────────────────

    def append(self, run_id: str, event: dict) -> dict:
        """Assign the next seq, store the event and wake every reader."""
        buf = self._get_or_create(run_id)
        with buf.cond:
            full = {"v": EVENT_VERSION, "seq": buf.next_seq, **event}
            buf.next_seq += 1
            buf.events.append(full)
            if full.get("type") in TERMINAL_EVENT_TYPES:
                buf.terminal_at = time.monotonic()
            buf.cond.notify_all()
            waiters = list(buf.waiters)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                # The reader's loop is closed; its finally discards it.
                pass
        return full

    # ── Reading ──────────────────────────────────────────────────────

    def last_seq(self, run_id: str) -> int:
        buf = self._get(run_id)
        if buf is None:
            return 0
        with buf.cond:
            return buf.last_seq

    def read(self, run_id: str, after: int, limit: int = READ_LIMIT) -> ReadResult:
        buf = self._get(run_id)
        if buf is None:
            return ReadResult(events=[], resync="buffer_missing")
        after = max(0, int(after))
        with buf.cond:
            first_seq = buf.events[0]["seq"] if buf.events else buf.next_seq
            if after < first_seq - 1:
                return ReadResult(events=[], resync="behind_buffer")
            start = after - first_seq + 1
            batch = list(itertools.islice(buf.events, start, start + max(1, limit)))
        # A batch ends at the run's terminal event, so a reader that stops
        # on ``terminal`` stops right after it, whatever came later.
        for i, event in enumerate(batch):
            if event.get("type") in TERMINAL_EVENT_TYPES:
                return ReadResult(events=batch[:i + 1], terminal=True)
        return ReadResult(events=batch)

    def wait(self, run_id: str, after: int, timeout: float) -> bool:
        """Block until an event with seq > after exists, or ``timeout`` passes."""
        buf = self._get(run_id)
        if buf is None:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        with buf.cond:
            while buf.last_seq <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                buf.cond.wait(remaining)
            return True

    async def wait_async(self, run_id: str, after: int, timeout: float) -> bool:
        """The asyncio form of ``wait`` for the ASGI stream."""
        buf = self._get(run_id)
        if buf is None:
            return False
        loop = asyncio.get_running_loop()
        ev = asyncio.Event()
        waiter = (loop, ev)
        with buf.cond:
            if buf.last_seq > after:
                return True
            buf.waiters.add(waiter)
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
            return True
        except TimeoutError:
            return False
        finally:
            with buf.cond:
                buf.waiters.discard(waiter)


def resync_event(run_id, message_id, *, after: int, reason: str) -> dict:
    """The control event that tells a reader to reload the message snapshot.

    Its seq is one past what the reader has, so a client that drops
    events with seq <= last applied still sees it. SSE sends it without
    an ``id:`` line so Last-Event-ID does not move.
    """
    return {
        "v": EVENT_VERSION,
        "seq": int(after) + 1,
        "type": EventType.RESYNC,
        "run_id": str(run_id),
        "message_id": str(message_id) if message_id else None,
        "ts": now_iso(),
        "reason": reason,
    }


_store_lock = threading.Lock()
_store: EventStore | None = None


def get_event_store() -> EventStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MemoryEventStore()
    return _store


def set_event_store(store: EventStore | None) -> None:
    """Replace the process store (tests); None restores a fresh default."""
    global _store
    with _store_lock:
        _store = store
