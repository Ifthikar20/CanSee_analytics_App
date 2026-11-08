"""One streamed model request: read it, translate it, survive a stop.

Always the beta stream: only it types ``fallback`` blocks and
``usage.iterations``. The SDK raises ValueError from the iterator when an
eagerly streamed tool input is not JSON at all; that is caught only
around ``next()``, so no other ValueError is mistaken for it. A stop
closes the stream from another thread (ActiveRun.cancel); whatever the
read then raises, a set cancel flag makes the outcome ``cancelled``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.agent.runtime.wire import block_to_wire

logger = logging.getLogger("apps")


@dataclass
class StreamOutcome:
    final: object | None = None
    error: BaseException | None = None
    invalid_json: bool = False
    cancelled: bool = False
    request_id: str | None = None
    # The response so far when it did not complete (dicts, as in wire).
    partial_blocks: list[dict] = field(default_factory=list)
    completed: set[int] = field(default_factory=set)
    # Usage known so far (message_start) when it did not complete.
    partial_usage: dict | None = None
    model: str | None = None


def _snapshot(stream):
    if stream is None:
        return None
    try:
        return stream.current_message_snapshot
    except AssertionError:
        return None


def stream_request(client, request: dict, *, translator, active, on_event=None) -> StreamOutcome:
    out = StreamOutcome()
    stream = None
    try:
        with client.beta.messages.stream(**request) as stream:
            active.attach(stream)
            out.request_id = stream.request_id
            iterator = iter(stream)
            while True:
                if active.cancelled:
                    out.cancelled = True
                    break
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                except ValueError as exc:
                    if active.cancelled:
                        out.cancelled = True
                    else:
                        out.invalid_json = True
                        out.error = exc
                    break
                translator.handle(event)
                if on_event is not None:
                    on_event()
            if active.cancelled:
                out.cancelled = True
            if not out.cancelled and not out.invalid_json:
                out.final = stream.get_final_message()
    except Exception as exc:
        if active.cancelled:
            out.cancelled = True
        else:
            out.error = exc
    finally:
        active.detach(stream)
        translator.finish()
    out.completed = set(translator.completed)
    out.model = translator.model
    if out.final is None:
        snapshot = _snapshot(stream)
        if snapshot is not None:
            try:
                out.partial_blocks = [block_to_wire(b) for b in snapshot.content]
            except Exception:
                logger.warning("agent: could not read a stopped response", exc_info=True)
                out.partial_blocks = []
        if translator.start_usage is not None:
            out.partial_usage = dict(translator.start_usage)
    return out
