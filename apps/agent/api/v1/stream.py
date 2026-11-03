"""The run event stream (SSE): GET /api/v1/agent/live/runs/<id>/stream/.

A plain Django view, not DRF (its JSON-only renderer would answer 406 to
``Accept: text/event-stream``). The JWT is checked by hand and the run
must belong to the caller. The database connection is closed before the
stream starts: the stream reads only the in-process event buffer.

Under ASGI the body is an async generator (one event loop serves every
open stream); under WSGI and the test client a sync generator. Frames:
``id: <seq>`` / ``event: <type>`` / ``data: <json>``; the stream opens
with ``retry: 3000`` and a comment, sends ``: ping`` every 15 seconds,
ends after the run's terminal event, and after AGENT_STREAM_MAX_SECONDS
sends ``event: reconnect`` so the client reconnects with Last-Event-ID.
A reader that is behind the buffer, or whose run has no buffer here,
gets a ``resync`` event (no ``id:`` line) and the stream ends.
"""
from __future__ import annotations

import json
import time

from django.core.handlers.asgi import ASGIRequest
from django.db import connection
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.agent.conf import agent_conf
from apps.agent.constants import EventType
from apps.agent.runtime.events import get_event_store, resync_event
from apps.agent.runtime.registry import start_process_services
from apps.agent.runtime.sweeper import interrupt_if_orphaned

RETRY_MS = 3000
PING_SECONDS = 15
OPENING = f"retry: {RETRY_MS}\n: stream open\n\n"
RECONNECT = "event: reconnect\ndata: {}\n\n"
PING = ": ping\n\n"


def frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    head = "" if event.get("type") == EventType.RESYNC else f"id: {event['seq']}\n"
    return f"{head}event: {event['type']}\ndata: {data}\n\n"


def _error(status: int, code: str, message: str) -> JsonResponse:
    return JsonResponse({"success": False, "error": {"code": code, "message": message}},
                        status=status)


def _after(request) -> int:
    raw = request.headers.get("Last-Event-ID") or request.GET.get("after") or "0"
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return 0


def sync_events(store, run_id: str, message_id: str, after: int, max_seconds: int):
    yield OPENING
    deadline = time.monotonic() + max_seconds
    cursor = after
    while True:
        result = store.read(run_id, cursor)
        if result.resync is not None:
            yield frame(resync_event(run_id, message_id, after=cursor, reason=result.resync))
            return
        for event in result.events:
            yield frame(event)
            cursor = event["seq"]
        if result.terminal:
            return
        if result.events:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield RECONNECT
            return
        if not store.wait(run_id, cursor, min(PING_SECONDS, remaining)):
            if deadline - time.monotonic() <= 0:
                yield RECONNECT
                return
            yield PING


async def async_events(store, run_id: str, message_id: str, after: int, max_seconds: int):
    yield OPENING
    deadline = time.monotonic() + max_seconds
    cursor = after
    while True:
        result = store.read(run_id, cursor)
        if result.resync is not None:
            yield frame(resync_event(run_id, message_id, after=cursor, reason=result.resync))
            return
        for event in result.events:
            yield frame(event)
            cursor = event["seq"]
        if result.terminal:
            return
        if result.events:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield RECONNECT
            return
        if not await store.wait_async(run_id, cursor, min(PING_SECONDS, remaining)):
            if deadline - time.monotonic() <= 0:
                yield RECONNECT
                return
            yield PING


@require_GET
def run_stream(request, run_id):
    from apps.agent.models import AgentRun

    conf = agent_conf()
    if not conf.enabled:
        return _error(503, "agent_disabled", "The agent workspace is turned off.")
    if not conf.process:
        return _error(503, "agent_unavailable",
                      "The agent service is not reachable right now. Please try again shortly.")
    start_process_services()
    try:
        auth = JWTAuthentication().authenticate(request)
    except APIException:
        auth = None
    if auth is None:
        return _error(401, "not_authenticated", "Your session has expired. Please log in again.")
    user = auth[0]
    run = (AgentRun.objects.filter(id=run_id, user=user)
           .values("id", "assistant_message_id").first())
    if run is None:
        return _error(404, "not_found", "Run not found.")
    run_key, message_id = str(run["id"]), str(run["assistant_message_id"])
    after = _after(request)
    store = get_event_store()
    if not store.has(run_key):
        # A run an earlier agent process left behind ends now, so the
        # reader's resync finds the reply settled.
        interrupt_if_orphaned(run_key)
    if not connection.in_atomic_block:
        connection.close()

    if isinstance(request, ASGIRequest):
        body = async_events(store, run_key, message_id, after, conf.stream_max_seconds)
    else:
        body = sync_events(store, run_key, message_id, after, conf.stream_max_seconds)
    response = StreamingHttpResponse(body, content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-store, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
