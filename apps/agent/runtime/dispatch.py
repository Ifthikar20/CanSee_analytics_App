"""Starting runs: inline (tests) or on this process's run executor.

Thread mode submits after the transaction that created the run commits,
to a process-wide pool of AGENT_MAX_CONCURRENT_RUNS threads; queued runs
wait for a free thread. Every runner thread closes its DB connections
when it ends.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connections, transaction
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.runtime.events import get_event_store
from apps.agent.runtime.registry import get_registry

logger = logging.getLogger("apps")

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=agent_conf().max_concurrent_runs,
                    thread_name_prefix="agent-run",
                )
    return _executor


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
        _get_executor().submit(_run_in_thread, run_id)

    transaction.on_commit(submit)
