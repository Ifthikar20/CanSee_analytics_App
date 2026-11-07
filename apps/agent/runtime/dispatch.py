"""Starting runs: inline (tests) or on this process's run threads.

Thread mode hands a run over after the transaction that created it
commits, to a process-wide pool of AGENT_MAX_CONCURRENT_RUNS threads;
queued runs wait for a free thread. The threads are daemon threads: an
exiting interpreter (a runserver reload, a shutdown) does not wait for
the runs in flight, which the next process or the sweeper interrupts,
and a run handed over meanwhile is still accepted instead of failing the
request that started it. A run that cannot be handed over is failed at
once rather than left queued. Every runner thread closes its DB
connections when a run ends.
"""
from __future__ import annotations

import logging
import queue
import threading

from django.db import connections, transaction
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.runtime.events import get_event_store
from apps.agent.runtime.registry import get_registry

logger = logging.getLogger("apps")


class RunThreads:
    """A fixed set of daemon threads that take run ids from one queue."""

    def __init__(self, size: int):
        self._queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.threads = [
            threading.Thread(target=self._work, name=f"agent-run-{i}", daemon=True)
            for i in range(size)
        ]
        for thread in self.threads:
            thread.start()

    def submit(self, run_id: str) -> None:
        self._queue.put(run_id)

    def _work(self) -> None:
        while True:
            _run_in_thread(self._queue.get())


_pool_lock = threading.Lock()
_pool: RunThreads | None = None


def _get_pool() -> RunThreads:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = RunThreads(agent_conf().max_concurrent_runs)
    return _pool


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

    def submit():
        try:
            _get_pool().submit(run_id)
        except Exception:
            # A registered, heart-beaten queued run would hold the chat
            # (409 run_in_progress) until this process exits.
            from apps.agent.runtime.runner import fail_unstarted

            logger.exception("agent run %s could not be handed to a thread", run_id)
            registry.unregister(run_id)
            fail_unstarted(run_id)

    transaction.on_commit(submit)
