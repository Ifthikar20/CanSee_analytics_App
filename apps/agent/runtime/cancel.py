"""Stopping a run. Idempotent: stopping a finished run changes nothing.

A queued run is cancelled on the spot. A running one gets
``cancel_requested_at``; the process executing it closes its stream
(right away when that is this process, else at its next heartbeat) and
the runner finishes the run as cancelled, keeping what is safe to keep.
"""
from __future__ import annotations

from django.utils import timezone

from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    EventType,
    MessageStatus,
    RunStatus,
)
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.registry import get_registry


def request_cancel(run) -> str:
    """Ask ``run`` to stop; returns its status after the request."""
    from apps.agent.models import AgentMessage, AgentRun

    if run.status not in ACTIVE_RUN_STATUSES:
        return run.status
    now = timezone.now()
    AgentRun.objects.filter(id=run.id, cancel_requested_at__isnull=True).update(
        cancel_requested_at=now,
    )
    cancelled = AgentRun.objects.filter(id=run.id, status=RunStatus.QUEUED).update(
        status=RunStatus.CANCELLED, finished_at=now,
    )
    if cancelled:
        AgentMessage.objects.filter(id=run.assistant_message_id).update(
            status=MessageStatus.CANCELLED, finished_at=now,
        )
        store = get_event_store()
        if store.has(run.id):
            store.append(str(run.id), {
                "type": EventType.RUN_CANCELLED,
                "run_id": str(run.id),
                "message_id": str(run.assistant_message_id),
                "ts": now_iso(),
            })
        get_registry().unregister(run.id)
        return RunStatus.CANCELLED
    get_registry().cancel_local(run.id)
    return AgentRun.objects.filter(id=run.id).values_list("status", flat=True).first() or run.status
