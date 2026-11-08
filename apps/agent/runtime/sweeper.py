"""Ending runs whose process went away.

A run is stale when its heartbeat (or, before its first one, its
creation) is older than AGENT_STALE_SECONDS. It moves to ``interrupted``
with a compare-and-set, its message is marked interrupted, open parts
are closed (a research's swarm part and its unfinished agents included,
and its version row), and its stored wire is left valid for the next turn
(wire.close_turn: an unanswered tool call gets a "Stopped before this
ran." result, a paused server tool call without its result goes). The
celery beat task runs it every minute; reading a chat runs it for that
chat; the agent process ends a run another executor left behind as soon
as a reader asks for it.

The same sweeps settle a reply still ``streaming`` whose run has ended
(``settle_stranded_messages``): it ends as its run did, so it never shows
a spinner forever and can be regenerated.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    ACTIVE_VERSION_STATUSES,
    LIVE_SWARM_STATUSES,
    TERMINAL_RUN_STATUSES,
    EventType,
    MessageStatus,
    PartType,
    RunStatus,
    VersionStatus,
)
from apps.agent.runtime import wire
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.parts import strip_nul

logger = logging.getLogger("apps")

_DONE = {"done", "error", "cancelled", "unavailable", "partial", "interrupted"}
_OPEN_TASK = ("queued", "running")
INTERRUPTED_VERSION_MESSAGE = "The agent process stopped before this research finished."
INTERRUPTED_MESSAGE = "The agent process stopped before this reply finished."
NOT_SAVED_MESSAGE = "This reply was not saved completely."
# A reply is settled only once its run ended this long ago.
STRANDED_GRACE_SECONDS = 30


def _interrupt_swarm(part: dict) -> dict:
    """A swarm part the process left open: interrupted, and each agent that
    had not finished stopped (rev + 1, as a task event would)."""
    from apps.agent.runtime.swarm.reporter import duration_ms_since

    now = now_iso()
    tasks = []
    for task in part.get("tasks") or []:
        task = dict(task)
        if task.get("status") in _OPEN_TASK:
            task.update(
                status="stopped", step="", ended_at=now,
                duration_ms=duration_ms_since(task.get("started_at")),
                error={"code": "interrupted", "message": "Interrupted"},
                rev=int(task.get("rev") or 0) + 1,
            )
        tasks.append(task)
    part.update(status="interrupted", ended_at=now, tasks=tasks)
    if part.get("started_at"):
        part["duration_ms"] = duration_ms_since(part["started_at"])
    return part


def _close_parts(parts: list[dict]) -> list[dict]:
    out = []
    for part in parts or []:
        part = dict(part)
        status = part.get("status")
        if part.get("type") == PartType.SWARM and status in LIVE_SWARM_STATUSES:
            out.append(_interrupt_swarm(part))
            continue
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


def _closed_turn(message) -> list:
    """The message's stored wire, valid for the next turn."""
    try:
        return wire.close_turn(wire.loads(message.wire))
    except ValueError:
        logger.warning("agent: unreadable wire on message %s", message.id)
        return wire.loads(message.wire) if message.wire else []


def interrupt_runs(run_ids, *, reason: str) -> int:
    """Move these runs (when still active) to interrupted. Returns how many."""
    from apps.agent.models import AgentMessage, AgentRun, AgentSwarmVersion

    count = 0
    store = get_event_store()
    runs = AgentRun.objects.filter(id__in=list(run_ids), status__in=ACTIVE_RUN_STATUSES)
    for run in runs.select_related("assistant_message"):
        now = timezone.now()
        message = run.assistant_message
        # The run and its message end together (see settle_stranded_messages).
        with transaction.atomic():
            updated = AgentRun.objects.filter(id=run.id, status=run.status).update(
                status=RunStatus.INTERRUPTED, finished_at=now, error_code="interrupted",
                error_message=INTERRUPTED_MESSAGE,
            )
            if not updated:
                continue
            AgentMessage.objects.filter(id=message.id).update(
                status=MessageStatus.INTERRUPTED,
                wire=wire.dumps(_closed_turn(message)),
                parts=strip_nul(_close_parts(message.parts)),
                error_code="interrupted",
                error_message=INTERRUPTED_MESSAGE,
                finished_at=now,
            )
            AgentSwarmVersion.objects.filter(
                run_id=run.id, status__in=ACTIVE_VERSION_STATUSES,
            ).update(
                status=VersionStatus.INTERRUPTED, finished_at=now, error_code="interrupted",
                error_message=INTERRUPTED_VERSION_MESSAGE,
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


def _stranded_ending(run) -> tuple[str, str, str]:
    """(message status, error code, error message) for the reply of an
    ended run: what the run ended as. A completed run whose reply was not
    saved ends interrupted, since its stored parts may be short."""
    if run.status == RunStatus.CANCELLED:
        return MessageStatus.CANCELLED, "", ""
    if run.status == RunStatus.FAILED:
        return (MessageStatus.FAILED, run.error_code or "internal_error",
                run.error_message or "Something went wrong.")
    if run.status == RunStatus.INTERRUPTED:
        return (MessageStatus.INTERRUPTED, run.error_code or "interrupted",
                run.error_message or INTERRUPTED_MESSAGE)
    return MessageStatus.INTERRUPTED, "interrupted", NOT_SAVED_MESSAGE


def settle_stranded_messages(qs=None, now=None) -> int:
    """Replies still ``streaming`` whose run ended at least
    STRANDED_GRACE_SECONDS ago (a write that failed after the run's) end
    as their run did, with their open parts closed and a wire the next
    turn can follow. Returns how many."""
    from apps.agent.models import AgentMessage, AgentRun

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=STRANDED_GRACE_SECONDS)
    runs = (qs if qs is not None else AgentRun.objects.all()).filter(
        Q(finished_at__lt=cutoff) | Q(finished_at__isnull=True, created_at__lt=cutoff),
        status__in=TERMINAL_RUN_STATUSES,
        assistant_message__status=MessageStatus.STREAMING,
    ).select_related("assistant_message")
    count = 0
    for run in runs:
        message = run.assistant_message
        status, code, text = _stranded_ending(run)
        count += AgentMessage.objects.filter(
            id=message.id, status=MessageStatus.STREAMING,
        ).update(
            status=status, error_code=code, error_message=text,
            wire=wire.dumps(_closed_turn(message)),
            parts=strip_nul(_close_parts(message.parts)),
            finished_at=run.finished_at or now,
        )
    if count:
        logger.warning("agent: %d reply(ies) left streaming after their run ended were settled",
                       count)
    return count


def sweep_stale_runs(now=None) -> int:
    from apps.agent.models import AgentRun

    interrupted = interrupt_runs(list(_stale(AgentRun.objects.all(), now)), reason="stale")
    settle_stranded_messages(now=now)
    return interrupted


def sweep_chat(chat_id, now=None) -> int:
    """The lazy sweep when a chat is read."""
    from apps.agent.models import AgentRun

    interrupted = interrupt_runs(list(_stale(AgentRun.objects.filter(chat_id=chat_id), now)),
                                 reason="stale")
    settle_stranded_messages(AgentRun.objects.filter(chat_id=chat_id), now=now)
    return interrupted
