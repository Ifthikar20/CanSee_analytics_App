"""Save an agent reply to Reports.

The report is the reply's Markdown plus a snapshot of the research behind
it: the Google results pages and pages it inspected (from the message's
research store), the AI engine checks (its engine_check parts) and the
prompt gaps it read. Nothing is recomputed; a report shows exactly what
the reply was written from.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from apps.agent.constants import (
    ARTIFACT_TITLE_MAX_LENGTH,
    AgentKey,
    ArtifactKind,
    MessageRole,
    MessageStatus,
    PartType,
    VersionSource,
)
from apps.agent.exceptions import AlreadySaved
from apps.agent.models import AgentArtifact, AgentMessage
from apps.agent.services.artifacts import add_version
from core.exceptions import CanseeException, ResourceNotFound

SAVABLE_STATUSES = (MessageStatus.COMPLETE, MessageStatus.TRUNCATED)
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class NotSavable(CanseeException):
    def __init__(self, message: str):
        super().__init__(message, code="invalid", status_code=400)


def save_report(user, website, *, message_id, title: str = "") -> AgentArtifact:
    """Create a report from one of the user's finished replies.

    Raises AlreadySaved (409, ``details.artifact_id``) when the reply's
    report already exists.
    """
    with transaction.atomic():
        message = (
            AgentMessage.objects.select_for_update(of=("self",))
            .filter(pk=message_id, chat__user=user, chat__website=website)
            .first()
        )
        if message is None:
            raise ResourceNotFound("Message not found.")
        if message.role != MessageRole.ASSISTANT:
            raise NotSavable("Only the agent's replies can be saved as reports.")
        if message.status not in SAVABLE_STATUSES:
            raise NotSavable("This reply isn't finished, so it can't be saved yet.")
        existing = message.saved_report
        if existing is not None and existing.deleted_at is None:
            raise AlreadySaved(artifact_id=existing.id)

        content = report_markdown(message.parts)
        if not content:
            raise NotSavable("This reply has no text to save.")
        data = research_snapshot(message)
        artifact = AgentArtifact.objects.create(
            website=website,
            kind=ArtifactKind.REPORT,
            agent_key=AgentKey.GAP_ANALYST,
            title=report_title(title, content, website),
            created_by=user,
            source_chat_id=message.chat_id,
            source_message=message,
        )
        add_version(
            artifact,
            content=content,
            data=data,
            summary=_summary(content, data),
            source=VersionSource.AGENT,
            created_by=user,
            run=getattr(message, "run", None),
            cost_usd=message.cost_usd,
        )
        message.saved_report = artifact
        message.save(update_fields=["saved_report"])
    return artifact


def report_markdown(parts) -> str:
    """The reply's text parts, in order."""
    texts = [
        str(part.get("text") or "").strip()
        for part in parts or []
        if isinstance(part, dict) and part.get("type") == PartType.TEXT
    ]
    return "\n\n".join(t for t in texts if t).replace("\x00", "")


def research_snapshot(message: AgentMessage) -> dict:
    """{serp, pages, engines, gaps} as captured while the reply was written.

    Captures appear in the order their parts appear in the reply; any
    capture no part points at follows, oldest first. Page captures carry
    no reader text.
    """
    research = message.research if isinstance(message.research, dict) else {}
    parts = [p for p in (message.parts or []) if isinstance(p, dict)]
    serp = _ordered(research.get("serp"), parts, PartType.SERP, "serp:")
    pages = [
        {k: v for k, v in capture.items() if k != "reader"}
        for capture in _ordered(research.get("pages"), parts, PartType.PAGE_INSPECT, "page:")
    ]
    gaps = research.get("gaps")
    return {
        "serp": serp,
        "pages": pages,
        "engines": [p for p in parts if p.get("type") == PartType.ENGINE_CHECK],
        "gaps": gaps if isinstance(gaps, list) else [],
    }


def report_title(given: str, content: str, website) -> str:
    """The given title, else the reply's first heading, else a dated default."""
    title = " ".join((given or "").split())
    if not title:
        title = _first_heading(content)
    if not title:
        today = timezone.localdate()
        title = f"SEO and GEO report · {website.name} · {today:%b} {today.day}, {today.year}"
    return title[:ARTIFACT_TITLE_MAX_LENGTH]


def _ordered(store, parts: list[dict], part_type: str, prefix: str) -> list[dict]:
    captures = store if isinstance(store, dict) else {}
    out, used = [], set()
    for part in parts:
        tab_id = str(part.get("tab_id") or "")
        if part.get("type") != part_type or not tab_id.startswith(prefix):
            continue
        capture_id = tab_id[len(prefix):]
        capture = captures.get(capture_id)
        if isinstance(capture, dict) and capture_id not in used:
            used.add(capture_id)
            out.append(capture)
    rest = [
        capture for capture_id, capture in captures.items()
        if capture_id not in used and isinstance(capture, dict)
    ]
    rest.sort(key=lambda c: (str(c.get("captured_at") or ""), c.get("page") or 0))
    return out + rest


def _first_heading(content: str) -> str:
    in_fence = False
    for line in content.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match:
            text = match.group(1).replace("**", "").replace("__", "").replace("`", "")
            text = " ".join(text.split())
            if text:
                return text
    return ""


def _summary(content: str, data: dict) -> str:
    def count(n: int, noun: str, suffix: str = "") -> str:
        return f"{n} {noun}{'' if n == 1 else 's'}{suffix}"

    bits = []
    if data["serp"]:
        bits.append(count(len(data["serp"]), "Google results page"))
    if data["pages"]:
        bits.append(count(len(data["pages"]), "page", " inspected"))
    if data["engines"]:
        bits.append(count(len(data["engines"]), "AI engine check"))
    sections = sum(1 for line in content.splitlines() if line.startswith("## "))
    if sections:
        bits.append(count(sections, "section"))
    return ", ".join(bits)
