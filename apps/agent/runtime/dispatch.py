"""Starting runs: inline (tests) or on this process's run threads.

Thread mode hands a run over after the transaction that created it
commits, to a process-wide pool of AGENT_MAX_CONCURRENT_RUNS threads;
queued runs wait for a free thread, first in first out. While a run
waits, ``queue_status`` gives its place in line and its event stream
gets ``run.queued`` ({position, ahead, slots}) each time that place
changes, so a reply that waits says so instead of looking stuck. The
threads are daemon threads: an
exiting interpreter (a runserver reload, a shutdown) does not wait for
the runs in flight, which the next process or the sweeper interrupts,
and a run handed over meanwhile is still accepted instead of failing the
request that started it. A run that cannot be handed over is failed at
once rather than left queued. Every runner thread closes its DB
connections when a run ends.
"""
from __future__ import annotations

import logging
import threading
from collections import deque

from django.db import connections, transaction
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import EventType
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.registry import get_registry

logger = logging.getLogger("apps")


class RunThreads:
    """A fixed set of daemon threads that take run ids from one line."""

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self._cond = threading.Condition()
        self._waiting: deque[str] = deque()
        # run id -> its assistant message id, for the run.queued envelope.
        self._messages: dict[str, str] = {}
        # run id -> the position its last run.queued announced.
        self._announced: dict[str, int] = {}
        self._busy = 0
        self.threads = [
            threading.Thread(target=self._work, name=f"agent-run-{i}", daemon=True)
            for i in range(self.size)
        ]
        for thread in self.threads:
            thread.start()

    def submit(self, run_id: str, message_id: str = "") -> None:
        with self._cond:
            self._waiting.append(run_id)
            self._messages[run_id] = message_id
            self._announce_locked()
            self._cond.notify()

    def withdraw(self, run_id: str) -> bool:
        """Take a run that was stopped while it waited out of the line."""
        with self._cond:
            try:
                self._waiting.remove(run_id)
            except ValueError:
                return False
            self._forget_locked(run_id)
            self._announce_locked()
            return True

    def queue_state(self, run_id: str) -> dict | None:
        """{position, ahead, slots} while ``run_id`` waits for a thread;
        None once a thread has it, or when one is free for it now."""
        with self._cond:
            try:
                index = self._waiting.index(run_id)
            except ValueError:
                return None
            return self._state_locked(index)

    def _state_locked(self, index: int) -> dict | None:
        # The first ``idle`` runs in line are taken by the free threads now.
        position = index + 1 - (self.size - self._busy)
        if position <= 0:
            return None
        return {"position": position, "ahead": position - 1, "slots": self.size}

    def _forget_locked(self, run_id: str) -> None:
        self._messages.pop(run_id, None)
        self._announced.pop(run_id, None)

    def _announce_locked(self) -> None:
        """run.queued for every waiting run whose place changed. Under the
        lock, so it always comes before the run.started of that run."""
        store = get_event_store()
        for index, run_id in enumerate(self._waiting):
            state = self._state_locked(index)
            if state is None or self._announced.get(run_id) == state["position"]:
                continue
            self._announced[run_id] = state["position"]
            try:
                store.append(run_id, {
                    "type": EventType.RUN_QUEUED,
                    "run_id": run_id,
                    "message_id": self._messages.get(run_id, ""),
                    "ts": now_iso(),
                    **state,
                })
            except Exception:
                logger.warning("agent run %s: run.queued could not be sent", run_id,
                               exc_info=True)

    def _take(self) -> str:
        with self._cond:
            while not self._waiting:
                self._cond.wait()
            run_id = self._waiting.popleft()
            self._forget_locked(run_id)
            self._busy += 1
            self._announce_locked()
            return run_id

    def _work(self) -> None:
        while True:
            run_id = self._take()
            try:
                _run_in_thread(run_id)
            finally:
                with self._cond:
                    self._busy -= 1


_pool_lock = threading.Lock()
_pool: RunThreads | None = None


def _get_pool() -> RunThreads:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = RunThreads(agent_conf().max_concurrent_runs)
    return _pool


def queue_status(run_id) -> dict | None:
    """Contract ``RunQueue``: {position, ahead, slots} while the run waits
    in this process for a free run thread, else None (it has a thread, it
    is not waiting, or this process does not run agents)."""
    pool = _pool
    if pool is None:
        return None
    return pool.queue_state(str(run_id))


def withdraw(run_id) -> None:
    """A queued run was stopped: it no longer waits for a thread."""
    pool = _pool
    if pool is not None:
        pool.withdraw(str(run_id))


def _run_in_thread(run_id: str) -> None:
    from apps.agent.runtime.runner import run_turn

    try:
        run_turn(run_id)
    except Exception:
        logger.exception("agent run %s crashed", run_id)
    finally:
        connections.close_all()


def dispatch_run(run_id) -> None:
    """Hand a queued run to its executor."""
    from apps.agent.models import AgentRun

    run_id = str(run_id)
    get_event_store().open(run_id)
    if agent_conf().run_mode == "inline":
        from apps.agent.runtime.runner import run_turn

        run_turn(run_id)
        return

    registry = get_registry()
    registry.ensure_heartbeat()
    # Queued runs heartbeat too, so a process that dies before a thread
    # frees up leaves runs the sweeper can end.
    registry.register(run_id)
    AgentRun.objects.filter(id=run_id).update(
        executor_id=registry.executor_id, heartbeat_at=timezone.now(),
    )
    message_id = (AgentRun.objects.filter(id=run_id)
                  .values_list("assistant_message_id", flat=True).first())

    def submit():
        try:
            _get_pool().submit(run_id, str(message_id or ""))
        except Exception:
            # A registered, heart-beaten queued run would hold the chat
            # (409 run_in_progress) until this process exits.
            from apps.agent.runtime.runner import fail_unstarted

            logger.exception("agent run %s could not be handed to a thread", run_id)
            registry.unregister(run_id)
            fail_unstarted(run_id)

    transaction.on_commit(submit)
