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
gets a ``resync`` event (no ``id:`` line) and the stream ends; so does a
reader that already has the run's terminal event (reason ``run_ended``),
instead of waiting for events that will never come. At each ping the
run's row is checked too: a run that ended without its terminal event
reaching this buffer (a run thread killed outside the runner, then swept)
also ends the stream with ``run_ended``.

One user holds at most MAX_OPEN_READS_PER_USER streams and long-polls
open in this process at once (429 ``too_many_streams`` beyond that), so
one account cannot take every connection the agent process accepts.
"""
from __future__ import annotations

import json
import threading
import time
import weakref
from collections import Counter

from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIRequest
from django.db import connection
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.agent.conf import agent_conf
from apps.agent.constants import TERMINAL_RUN_STATUSES, EventType
from apps.agent.exceptions import TooManyStreams
from apps.agent.runtime.events import get_event_store, resync_event
from apps.agent.runtime.registry import start_process_services
from apps.agent.runtime.sweeper import interrupt_if_orphaned

RETRY_MS = 3000
PING_SECONDS = 15
OPENING = f"retry: {RETRY_MS}\n: stream open\n\n"
RECONNECT = "event: reconnect\ndata: {}\n\n"
PING = ": ping\n\n"
RUN_ENDED = "run_ended"
MAX_OPEN_READS_PER_USER = 8
# How long a run whose row has ended may still take to put its terminal
# event in the buffer (the runner saves the row first, then emits).
SETTLE_GRACE_SECONDS = 2.0


class OpenReads:
    """Event readers (streams and long-polls) each user holds open in this
    process."""

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._open: Counter = Counter()

    def acquire(self, user_id):
        """A function that gives the slot back (safe to call twice), or
        None when the user already holds ``limit`` readers."""
        key = str(user_id)
        with self._lock:
            if self._open[key] >= self.limit:
                return None
            self._open[key] += 1
        held = [True]

        def release() -> None:
            with self._lock:
                if held[0]:
                    held[0] = False
                    self._open[key] -= 1
                    if self._open[key] <= 0:
                        del self._open[key]

        return release

    def held(self, user_id) -> int:
        with self._lock:
            return self._open[str(user_id)]


open_reads = OpenReads(MAX_OPEN_READS_PER_USER)


def run_ended(store, run_id: str, after: int) -> bool:
    """True when the run's terminal event is the newest one in its buffer
    and a reader at ``after`` already has it: nothing more will come."""
    last = store.last_seq(run_id)
    if not last or after < last:
        return False
    return store.read(run_id, last - 1, limit=1).terminal


def run_settled(run_id: str) -> bool:
    """True when the run's row has a terminal status. Read at a ping, after
    a quiet interval; the connection is closed again, so an open stream
    holds none between pings. A failed read counts as not settled."""
    from apps.agent.models import AgentRun

    try:
        status = AgentRun.objects.filter(id=run_id).values_list("status", flat=True).first()
    except Exception:
        return False
    finally:
        if not connection.in_atomic_block:
            connection.close()
    return status in TERMINAL_RUN_STATUSES


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
        if run_ended(store, run_id, cursor):
            yield frame(resync_event(run_id, message_id, after=cursor, reason=RUN_ENDED))
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield RECONNECT
            return
        if not store.wait(run_id, cursor, min(PING_SECONDS, remaining)):
            if deadline - time.monotonic() <= 0:
                yield RECONNECT
                return
            if run_settled(run_id) and not store.wait(run_id, cursor, SETTLE_GRACE_SECONDS):
                # The row ended, and nothing more reached this buffer.
                yield frame(resync_event(run_id, message_id, after=cursor, reason=RUN_ENDED))
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
        if run_ended(store, run_id, cursor):
            yield frame(resync_event(run_id, message_id, after=cursor, reason=RUN_ENDED))
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield RECONNECT
            return
        if not await store.wait_async(run_id, cursor, min(PING_SECONDS, remaining)):
            if deadline - time.monotonic() <= 0:
                yield RECONNECT
                return
            if (await sync_to_async(run_settled)(run_id)
                    and not await store.wait_async(run_id, cursor, SETTLE_GRACE_SECONDS)):
                # The row ended, and nothing more reached this buffer.
                yield frame(resync_event(run_id, message_id, after=cursor, reason=RUN_ENDED))
                return
            yield PING


def _sync_held(body, release):
    """``body``, giving the reader's slot back when the stream ends."""
    try:
        yield from body
    finally:
        release()


async def _async_held(body, release):
    try:
        async for chunk in body:
            yield chunk
    finally:
        release()


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
    release = open_reads.acquire(user.id)
    if release is None:
        refused = TooManyStreams()
        response = _error(refused.status_code, refused.code, refused.message)
        response["Retry-After"] = str(RETRY_MS // 1000)
        return response
    if not connection.in_atomic_block:
        connection.close()

    if isinstance(request, ASGIRequest):
        body = _async_held(
            async_events(store, run_key, message_id, after, conf.stream_max_seconds), release)
    else:
        body = _sync_held(
            sync_events(store, run_key, message_id, after, conf.stream_max_seconds), release)
    # A body that is never read never reaches its finally.
    weakref.finalize(body, release)
    response = StreamingHttpResponse(body, content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-store, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
