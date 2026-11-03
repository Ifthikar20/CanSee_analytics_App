"""The runs this process executes: cancel flags, live streams, heartbeats.

``executor_id`` ("<hostname>:<pid>:<boot_ts>") names this process on
every run it takes. A daemon thread (thread mode only) heartbeats the
active runs every AGENT_HEARTBEAT_SECONDS with a compare-and-set update;
a run whose row no longer matches (the sweeper marked it interrupted) is
stopped locally. The same thread picks up stop requests that reached
another process and refreshes the cache key ``status/`` reads to report
the agent process as live.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

from django.core.cache import cache
from django.db import connections
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import ACTIVE_RUN_STATUSES, RunStatus

logger = logging.getLogger("apps")

LIVE_HEARTBEAT_CACHE_KEY = "agent:live:heartbeat"
LIVE_HEARTBEAT_TTL = 30
_BOOT_TS = int(time.time())


def make_executor_id() -> str:
    host = (socket.gethostname() or "host")[:40]
    return f"{host}:{os.getpid()}:{_BOOT_TS}"[:80]


class ActiveRun:
    """One run in this process: its cancel flag and the stream it reads."""

    def __init__(self, run_id: str):
        self.run_id = str(run_id)
        self.cancel_event = threading.Event()
        # The run's row was taken over (interrupted) by someone else.
        self.lost = False
        self._lock = threading.Lock()
        self._stream = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def attach(self, stream) -> None:
        with self._lock:
            self._stream = stream
            close_now = self.cancel_event.is_set()
        if close_now:
            self._close(stream)

    def detach(self, stream=None) -> None:
        with self._lock:
            if stream is None or self._stream is stream:
                self._stream = None

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            self._close(stream)

    @staticmethod
    def _close(stream) -> None:
        try:
            stream.close()
        except Exception:
            logger.debug("agent: closing a stream failed", exc_info=True)


class RunRegistry:
    def __init__(self):
        self.executor_id = make_executor_id()
        self._lock = threading.Lock()
        self._runs: dict[str, ActiveRun] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._recovered = False

    # ── Runs ─────────────────────────────────────────────────────────

    def register(self, run_id) -> ActiveRun:
        key = str(run_id)
        with self._lock:
            active = self._runs.get(key)
            if active is None:
                active = ActiveRun(key)
                self._runs[key] = active
            return active

    def get(self, run_id) -> ActiveRun | None:
        with self._lock:
            return self._runs.get(str(run_id))

    def unregister(self, run_id) -> None:
        with self._lock:
            self._runs.pop(str(run_id), None)

    def active_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def cancel_local(self, run_id) -> bool:
        """Stop a run this process executes. False when it is not here."""
        active = self.get(run_id)
        if active is None:
            return False
        active.cancel()
        return True

    # ── Recovery and heartbeat ───────────────────────────────────────

    def recover_orphans(self) -> int:
        """Once per process: runs an earlier process on this host left
        running (same hostname, another pid or boot) become interrupted."""
        if self._recovered:
            return 0
        self._recovered = True
        from apps.agent.runtime.sweeper import interrupt_runs

        host = self.executor_id.split(":", 1)[0]
        from apps.agent.models import AgentRun

        orphan_ids = list(
            AgentRun.objects.filter(
                status__in=ACTIVE_RUN_STATUSES, executor_id__startswith=f"{host}:",
            ).exclude(executor_id=self.executor_id).values_list("id", flat=True)
        )
        if not orphan_ids:
            return 0
        return interrupt_runs(orphan_ids, reason="process_restarted")

    def beat(self) -> None:
        """One heartbeat: refresh active runs, notice lost runs and remote stops."""
        from apps.agent.models import AgentRun

        try:
            cache.set(LIVE_HEARTBEAT_CACHE_KEY, timezone.now().isoformat(), LIVE_HEARTBEAT_TTL)
        except Exception:
            logger.debug("agent: live heartbeat cache write failed", exc_info=True)
        ids = self.active_ids()
        if not ids:
            return
        now = timezone.now()
        for run_id in ids:
            updated = AgentRun.objects.filter(
                id=run_id, status__in=ACTIVE_RUN_STATUSES, executor_id=self.executor_id,
            ).update(heartbeat_at=now)
            if updated:
                continue
            active = self.get(run_id)
            if active is None or active.cancelled:
                continue
            status = AgentRun.objects.filter(id=run_id).values_list("status", flat=True).first()
            if status not in ACTIVE_RUN_STATUSES:
                # Someone else ended the row (the sweeper, or a stop while
                # queued): this process must not write the run any more.
                active.lost = status == RunStatus.INTERRUPTED
                active.cancel()
        requested = AgentRun.objects.filter(
            id__in=ids, cancel_requested_at__isnull=False,
        ).values_list("id", flat=True)
        for run_id in requested:
            self.cancel_local(run_id)

    def ensure_heartbeat(self) -> None:
        """Start the daemon heartbeat thread (thread mode), once."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="agent-heartbeat", daemon=True,
            )
            self._thread.start()

    def _loop(self) -> None:
        try:
            self.recover_orphans()
        except Exception:
            logger.warning("agent: orphan recovery failed", exc_info=True)
        while not self._stop.is_set():
            try:
                self.beat()
            except Exception:
                logger.warning("agent: heartbeat failed", exc_info=True)
            finally:
                connections.close_all()
            self._stop.wait(max(1, agent_conf().heartbeat_seconds))

    def shutdown(self) -> None:
        self._stop.set()


_registry_lock = threading.Lock()
_registry: RunRegistry | None = None


def start_process_services() -> None:
    """In the agent process (thread mode): start the heartbeat, which also
    recovers this host's orphaned runs and keeps ``status/`` reporting the
    process as live. Idempotent and cheap; call it at process start."""
    conf = agent_conf()
    if conf.enabled and conf.process and conf.run_mode == "thread":
        get_registry().ensure_heartbeat()


def get_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = RunRegistry()
    return _registry


def reset_registry() -> None:
    """A fresh registry (tests)."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.shutdown()
        _registry = None
