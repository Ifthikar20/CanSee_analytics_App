"""Ending runs whose process went away.

A run is stale when its heartbeat (or, before its first one, its
creation) is older than AGENT_STALE_SECONDS. It moves to ``interrupted``
with a compare-and-set, its message is marked interrupted, open parts
are closed, and its stored wire is left valid for the next turn
(wire.close_turn: an unanswered tool call gets a "Stopped before this
ran." result, a paused server tool call without its result goes). The
celery beat task runs it every minute; reading a chat runs it for that
chat; the agent process ends a run another executor left behind as soon
as a reader asks for it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import ACTIVE_RUN_STATUSES, EventType, MessageStatus, RunStatus
from apps.agent.runtime import wire
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.parts import strip_nul

logger = logging.getLogger("apps")

_DONE = {"done", "error", "cancelled", "unavailable"}


def _close_parts(parts: list[dict]) -> list[dict]:
    out = []
    for part in parts or []:
        part = dict(part)
        status = part.get("status")
        if part.get("type") == "thinking" and not part.get("ended_at"):
            part["ended_at"] = now_iso()
        elif status is not None and status not in _DONE:
            part["status"] = "error"
            if part.get("type") in ("serp", "page_inspect"):
                part["error"] = {"code": "interrupted", "message": "Interrupted"}
            elif part.get("type") in ("web_search", "web_fetch"):
                part["error_code"] = "interrupted"
            else:
                part["error"] = "Interrupted"
        out.append(part)
    return out


def interrupt_runs(run_ids, *, reason: str) -> int:
    """Move these runs (when still active) to interrupted. Returns how many."""
    from apps.agent.models import AgentMessage, AgentRun

    count = 0
    store = get_event_store()
    runs = AgentRun.objects.filter(id__in=list(run_ids), status__in=ACTIVE_RUN_STATUSES)
    for run in runs.select_related("assistant_message"):
        now = timezone.now()
        updated = AgentRun.objects.filter(id=run.id, status=run.status).update(
            status=RunStatus.INTERRUPTED, finished_at=now, error_code="interrupted",
            error_message="The agent process stopped before this reply finished.",
        )
        if not updated:
            continue
        message = run.assistant_message
        try:
            turn = wire.close_turn(wire.loads(message.wire))
        except ValueError:
            logger.warning("agent: unreadable wire on message %s", message.id)
            turn = wire.loads(message.wire) if message.wire else []
        AgentMessage.objects.filter(id=message.id).update(
            status=MessageStatus.INTERRUPTED,
            wire=wire.dumps(turn),
            parts=strip_nul(_close_parts(message.parts)),
            error_code="interrupted",
            error_message="The agent process stopped before this reply finished.",
            finished_at=now,
        )
        if store.has(run.id):
            store.append(str(run.id), {
                "type": EventType.RUN_INTERRUPTED,
                "run_id": str(run.id),
                "message_id": str(message.id),
                "ts": now_iso(),
                "reason": reason,
            })
        count += 1
    if count:
        logger.info("agent: %d run(s) interrupted (%s)", count, reason)
    return count


def interrupt_if_orphaned(run_id) -> bool:
    """In the agent process: end a run no process will finish.

    The run is active, another executor took it, and this process neither
    runs it nor buffers its events. There is one agent process, so that
    executor is gone (a restart or a deploy). Ending the run now lets a
    reader's resync settle on the interrupted reply at once instead of
    retrying until the heartbeat goes stale. A run not yet handed to an
    executor (blank executor_id) is left alone.
    """
    from apps.agent.models import AgentRun
    from apps.agent.runtime.registry import get_registry

    registry = get_registry()
    if registry.get(run_id) is not None or get_event_store().has(run_id):
        return False
    owner = (AgentRun.objects.filter(id=run_id, status__in=ACTIVE_RUN_STATUSES)
             .values_list("executor_id", flat=True).first())
    if not owner or owner == registry.executor_id:
        return False
    return interrupt_runs([run_id], reason="process_restarted") > 0


def _stale(qs, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=agent_conf().stale_seconds)
    return (qs.filter(status__in=ACTIVE_RUN_STATUSES)
            .annotate(last_beat=Coalesce(F("heartbeat_at"), F("created_at")))
            .filter(last_beat__lt=cutoff)
            .values_list("id", flat=True))


def sweep_stale_runs(now=None) -> int:
    from apps.agent.models import AgentRun

    return interrupt_runs(list(_stale(AgentRun.objects.all(), now)), reason="stale")


def sweep_chat(chat_id, now=None) -> int:
    """The lazy sweep when a chat is read."""
    from apps.agent.models import AgentRun

    return interrupt_runs(list(_stale(AgentRun.objects.filter(chat_id=chat_id), now)),
                          reason="stale")
