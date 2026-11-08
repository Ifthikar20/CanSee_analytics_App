"""Chat titles: the first user message, then a model-written name.

When the first message is sent, the chat gets the message cut to 60
characters on a word boundary (``auto``). The first turn then asks the
model for a short title (effort low, structured JSON output) and, when
that works, stores it (``model``) and, while the reply is still running,
emits ``chat.updated``. A title the user set is never overwritten.
"""
from __future__ import annotations

import json
import logging
import time

from apps.agent.constants import GENERATED_TITLE_MAX_CHARS, EventType, TitleSource
from apps.agent.llm.client import get_client
from apps.agent.llm.request import build_title_request
from apps.agent.llm.usage import record_request_usage

logger = logging.getLogger("apps")


def fallback_title(text: str, limit: int = GENERATED_TITLE_MAX_CHARS) -> str:
    """``text`` on one line, cut to ``limit`` characters on a word boundary."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    if " " in cut and flat[limit] != " ":
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-")


def _clean(title: str) -> str:
    title = " ".join(str(title or "").split()).strip().strip("\"'").rstrip(".!?;:,")
    return fallback_title(title)


def user_text(message) -> str:
    for part in message.parts or []:
        if part.get("type") == "text" and part.get("text"):
            return part["text"]
    return ""


def generate_title(run, *, emitter=None) -> str | None:
    """Ask the model for the chat's title; store and announce it. Returns
    the title, or None when the model gave none (the fallback stays)."""
    from apps.agent.models import AgentChat

    text = user_text(run.user_message)
    if not text.strip():
        return None
    request = build_title_request(text)
    started = time.monotonic()
    try:
        response = get_client().messages.create(**request)
    except Exception:
        logger.info("agent: title request failed for chat %s", run.chat_id, exc_info=True)
        return None
    try:
        record_request_usage(
            run, response.usage, model=request["model"], role="title", step="title",
            request_id=getattr(response, "_request_id", "") or "",
            stop_reason=response.stop_reason or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        logger.warning("agent: title usage was not recorded", exc_info=True)
    if response.stop_reason == "refusal":
        return None
    raw = "".join(getattr(b, "text", "") for b in response.content
                  if getattr(b, "type", "") == "text")
    try:
        title = _clean(json.loads(raw).get("title", ""))
    except (ValueError, AttributeError):
        return None
    if not title:
        return None
    updated = (AgentChat.objects.filter(id=run.chat_id)
               .exclude(title_source=TitleSource.USER)
               .update(title=title, title_source=TitleSource.MODEL))
    if updated and emitter is not None:
        # A title that comes after the reply ended is only stored: the run's
        # terminal event stays its last one.
        emitter.emit_before_end(EventType.CHAT_UPDATED, chat_id=str(run.chat_id), title=title)
    return title if updated else None
