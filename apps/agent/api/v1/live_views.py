"""Endpoints served by the agent process (/api/v1/agent/live/).

Starting runs (send, regenerate, edit, continue, a research re-run),
stopping one, and the long-poll event feed. Every view refuses to work
unless AGENT_ENABLED is on (503 agent_disabled) and this is the agent
process (503 agent_unavailable), so a gunicorn worker never starts runner
threads or holds a poll open. Starting a run needs member access to the
website (viewers get 403, as for every other agent write); runs are only
ever visible to the user who started them.
"""
from __future__ import annotations

import uuid

from django.db import connection
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.agent.api.v1.serializers import (
    MessageSerializer,
    RunSerializer,
    SwarmRerunSerializer,
    run_queue,
)
from apps.agent.api.v1.stream import open_reads, run_ended
from apps.agent.api.v1.views import AgentAPIView, _validated
from apps.agent.conf import agent_conf
from apps.agent.constants import TERMINAL_RUN_STATUSES
from apps.agent.exceptions import SwarmDisabled, TooManyStreams
from apps.agent.runtime.cancel import request_cancel
from apps.agent.runtime.events import get_event_store, resync_event
from apps.agent.runtime.registry import start_process_services
from apps.agent.runtime.sweeper import interrupt_if_orphaned
from apps.agent.services import runs as run_service
from apps.agent.services import swarms as swarm_service
from core.exceptions import CanseeException, ResourceNotFound

LONG_POLL_MAX_SECONDS = 25


class _LiveView(AgentAPIView):
    """The agent API gates (signed in, AGENT_ENABLED, the agent process),
    then the process services this process runs (heartbeat, recovery)."""

    live = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        start_process_services()


def _uuid_or_none(value, field: str):
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise CanseeException(f"{field} is not a valid id.", code="invalid",
                              status_code=400) from None


def _started_payload(started, *, with_user: bool = True) -> dict:
    data = {
        "assistant_message": MessageSerializer(started.assistant_message).data,
        "run": RunSerializer(started.run).data,
    }
    if with_user:
        data["user_message"] = (
            MessageSerializer(started.user_message).data if started.user_message else None
        )
    return data


class SendMessageView(_LiveView):
    """POST a user message; the reply runs in the background (202).

    Body: ``{text, parent_id?, agent?, template?, engines?}``. ``template``
    is a report template key (GET report-templates/) applied to this
    reply. ``engines`` (public engine keys) limits the AI engines this
    reply's research asks.
    """

    def post(self, request, website_id, chat_id):
        from apps.agent.models import AgentChat

        website = self.website(write=True)
        chat = AgentChat.objects.filter(id=chat_id, website=website, user=request.user).first()
        if chat is None:
            raise ResourceNotFound("Chat not found.")
        body = request.data if isinstance(request.data, dict) else {}
        started = run_service.start_send(
            user=request.user,
            chat=chat,
            text=body.get("text"),
            parent_id=_uuid_or_none(body.get("parent_id"), "parent_id"),
            agent=body.get("agent"),
            idempotency_key=request.headers.get("Idempotency-Key"),
            template=body.get("template"),
            engines=body.get("engines"),
        )
        return Response(_started_payload(started), status=status.HTTP_202_ACCEPTED)


class RegenerateMessageView(_LiveView):
    """POST: a new reply to the same user message, as a sibling."""

    def post(self, request, website_id, message_id):
        website = self.website(write=True)
        started = run_service.start_regenerate(
            user=request.user, website=website, message_id=message_id,
        )
        return Response(_started_payload(started, with_user=False),
                        status=status.HTTP_202_ACCEPTED)


class EditMessageView(_LiveView):
    """POST {text, template?}: a new version of a user message, and its
    reply. Without ``template`` the edit keeps the message's report
    template; null or "" drops it."""

    def post(self, request, website_id, message_id):
        website = self.website(write=True)
        body = request.data if isinstance(request.data, dict) else {}
        extra = {"template": body.get("template")} if "template" in body else {}
        started = run_service.start_edit(
            user=request.user, website=website, message_id=message_id, text=body.get("text"),
            **extra,
        )
        return Response(_started_payload(started), status=status.HTTP_202_ACCEPTED)


class ContinueMessageView(_LiveView):
    """POST: continue a reply that was cut off (status truncated)."""

    def post(self, request, website_id, message_id):
        website = self.website(write=True)
        started = run_service.start_continue(
            user=request.user, website=website, message_id=message_id,
        )
        return Response(_started_payload(started), status=status.HTTP_202_ACCEPTED)


class SwarmRerunView(_LiveView):
    """POST {scope, category?, task_id?}: run a research again, all of it,
    one category or one agent, as a new version in a new turn (202)."""

    def post(self, request, website_id, swarm_id):
        website = self.website(write=True)
        if not agent_conf().swarm_enabled:
            raise SwarmDisabled()
        data = _validated(SwarmRerunSerializer, request.data)
        started = swarm_service.start_rerun(
            request.user, website, swarm_id, scope=data["scope"],
            category=data.get("category"), task_id=data.get("task_id"),
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return Response(
            {**_started_payload(started), "swarm_version": started.extra["swarm_version"]},
            status=status.HTTP_202_ACCEPTED,
        )


def _own_run(user, run_id):
    from apps.agent.models import AgentRun

    run = AgentRun.objects.filter(id=run_id, user=user).first()
    if run is None:
        raise ResourceNotFound("Run not found.")
    return run


class CancelRunView(_LiveView):
    """POST: stop a run. Idempotent; returns the run's status."""

    def post(self, request, run_id):
        run = _own_run(request.user, run_id)
        return Response({"status": request_cancel(run)})


def _int_param(value, default: int, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class RunEventsView(_LiveView):
    """GET ?after=&wait=: the run's events after ``after``, waiting up to
    ``wait`` (max 25) seconds for new ones. Not enveloped:
    {events, next_after, terminal, run_status, queue}; ``queue`` is the
    contract RunQueue while the run waits for a free thread. A run that has already
    ended answers at once; a user holds at most MAX_OPEN_READS_PER_USER
    polls and streams open at a time (429 too_many_streams)."""

    renderer_classes = [JSONRenderer]

    def get(self, request, run_id):
        run = _own_run(request.user, run_id)
        after = _int_param(request.query_params.get("after"), 0, low=0, high=2**62)
        wait = _int_param(request.query_params.get("wait"), LONG_POLL_MAX_SECONDS,
                          low=0, high=LONG_POLL_MAX_SECONDS)
        store = get_event_store()
        if not store.has(run.id):
            # A run an earlier agent process left behind ends now, so the
            # reader's resync finds the reply settled.
            interrupt_if_orphaned(run.id)
        release = open_reads.acquire(request.user.id)
        if release is None:
            raise TooManyStreams()
        try:
            result = store.read(run.id, after)
            ended = run.status in TERMINAL_RUN_STATUSES or run_ended(store, str(run.id), after)
            if not result.events and result.resync is None and wait and not ended:
                # The wait holds no database connection.
                if not connection.in_atomic_block:
                    connection.close()
                store.wait(run.id, after, wait)
                result = store.read(run.id, after)
        finally:
            release()
        run.refresh_from_db(fields=["status"])
        queue = run_queue(run)
        if result.resync is not None:
            event = resync_event(run.id, run.assistant_message_id, after=after,
                                 reason=result.resync)
            return Response({"events": [event], "next_after": after, "terminal": False,
                             "run_status": run.status, "queue": queue})
        events = result.events
        # Nothing more will come once the run has ended and nothing is left.
        finished = not events and run.status in TERMINAL_RUN_STATUSES
        return Response({
            "events": events,
            "next_after": events[-1]["seq"] if events else after,
            "terminal": result.terminal or finished,
            "run_status": run.status,
            "queue": queue,
        })
