"""Chats and their message trees (gunicorn side).

A chat belongs to the user who started it; every lookup here filters on
that user as well as the website, so another member of the same project
gets a 404. Messages form a tree: regenerate adds an assistant sibling,
edit adds a user sibling, and ``current_leaf`` marks the branch on screen.
Runs and messages are written by the agent process; this module reads
them, renames, switches branches, deletes and records feedback.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    CHAT_TITLE_MAX_LENGTH,
    DEFAULT_ENTRY_AGENT,
    FEEDBACK_DOWN,
    FEEDBACK_UP,
    MessageRole,
    TitleSource,
)
from apps.agent.exceptions import RunInProgress
from apps.agent.models import AgentChat, AgentMessage, AgentRun
from core.exceptions import CanseeException, ResourceNotFound

CHATS_PAGE_SIZE = 30
SEARCH_MAX_LENGTH = 200
PURGE_BATCH = 200


class InvalidCursor(CanseeException):
    def __init__(self):
        super().__init__(
            "The chat list position is not valid. Load the list again.",
            code="invalid", status_code=400,
        )


@dataclass
class ChatView:
    """What GET chat returns: the visible branch and the active run."""

    chat: AgentChat
    leaf_id: uuid.UUID | None
    messages: list[AgentMessage]
    sibling_ids: dict[uuid.UUID, list[uuid.UUID]]
    active_run: AgentRun | None


# ── Listing and lookup ────────────────────────────────────────────────────


def chats_for(user, website):
    """The user's chats in ``website`` (never anyone else's)."""
    return AgentChat.objects.filter(website=website, user=user)


def list_chats(user, website, *, q: str = "", cursor: str = "") -> tuple[list[AgentChat], str | None]:
    """Newest activity first, CHATS_PAGE_SIZE per page, keyset-paginated.

    A chat appears once it has a message: one whose first send failed is
    not listed. ``q`` matches the title and the preview.
    """
    active_run = AgentRun.objects.filter(
        chat=OuterRef("pk"), status__in=ACTIVE_RUN_STATUSES,
    ).values("id")[:1]
    qs = (
        chats_for(user, website)
        .filter(Exists(AgentMessage.objects.filter(chat=OuterRef("pk"))))
        .annotate(
            activity_at=Coalesce("last_message_at", "created_at"),
            active_run_id=Subquery(active_run),
        )
        .order_by("-activity_at", "-id")
    )
    q = (q or "").strip()[:SEARCH_MAX_LENGTH]
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(preview__icontains=q))
    if cursor:
        at, last_id = _decode_cursor(cursor)
        qs = qs.filter(Q(activity_at__lt=at) | Q(activity_at=at, id__lt=last_id))
    page = list(qs[: CHATS_PAGE_SIZE + 1])
    if len(page) <= CHATS_PAGE_SIZE:
        return page, None
    page = page[:CHATS_PAGE_SIZE]
    return page, _encode_cursor(page[-1].activity_at, page[-1].id)


def get_chat(user, website, chat_id) -> AgentChat:
    chat = chats_for(user, website).filter(pk=chat_id).first()
    if chat is None:
        raise ResourceNotFound("Chat not found.")
    return chat


def create_chat(user, website, *, entry_agent: str = DEFAULT_ENTRY_AGENT) -> AgentChat:
    return AgentChat.objects.create(
        website=website,
        user=user,
        entry_agent=entry_agent,
        snapshot=_orchestrator_snapshot(),
    )


def _orchestrator_snapshot():
    """The prompt snapshot a new chat pins (the orchestrator's current one)."""
    from apps.agent.llm.snapshot import get_or_create_snapshot

    return get_or_create_snapshot(DEFAULT_ENTRY_AGENT)


# ── The visible branch ────────────────────────────────────────────────────


def chat_view(chat: AgentChat) -> ChatView:
    """The path root -> leaf, sibling ids for each node, and the active run.

    Reading a chat first lets the sweeper settle a run whose process died,
    so a stale "running" state does not stick in the UI.
    """
    _sweep_stale_runs(chat.id)
    nodes = list(chat.messages.values_list("id", "parent_id", "created_at"))
    children = _children_by_parent(nodes)
    parent_of = {node_id: parent_id for node_id, parent_id, _ in nodes}

    leaf_id = chat.current_leaf_id if chat.current_leaf_id in parent_of else None
    if leaf_id is None and nodes:
        roots = children.get(None, [])
        leaf_id = _newest_leaf(roots[-1], children) if roots else None

    path_ids: list[uuid.UUID] = []
    node_id = leaf_id
    while node_id is not None and len(path_ids) <= len(nodes):
        path_ids.append(node_id)
        node_id = parent_of.get(node_id)
    path_ids.reverse()

    rows = {
        m.id: m
        for m in AgentMessage.objects.filter(id__in=path_ids)
        .defer("wire")
        .select_related("run")
    }
    messages = [rows[i] for i in path_ids if i in rows]
    sibling_ids = {m.id: children.get(m.parent_id, [m.id]) for m in messages}
    active_run = chat.runs.filter(status__in=ACTIVE_RUN_STATUSES).first()
    return ChatView(
        chat=chat, leaf_id=leaf_id, messages=messages,
        sibling_ids=sibling_ids, active_run=active_run,
    )


def sibling_ids_for(message: AgentMessage) -> list[uuid.UUID]:
    """Ids of ``message`` and its siblings, oldest first."""
    return list(
        AgentMessage.objects.filter(chat_id=message.chat_id, parent_id=message.parent_id)
        .order_by("created_at", "id")
        .values_list("id", flat=True)
    )


def _children_by_parent(nodes) -> dict:
    children: dict = {}
    for node_id, parent_id, _created_at in sorted(nodes, key=lambda n: (n[2], n[0])):
        children.setdefault(parent_id, []).append(node_id)
    return children


def _newest_leaf(node_id, children: dict):
    seen = set()
    while children.get(node_id) and node_id not in seen:
        seen.add(node_id)
        node_id = children[node_id][-1]
    return node_id


# ── Changes ───────────────────────────────────────────────────────────────


def rename_chat(chat: AgentChat, title: str) -> AgentChat:
    chat.title = title.strip()[:CHAT_TITLE_MAX_LENGTH]
    chat.title_source = TitleSource.USER
    chat.save(update_fields=["title", "title_source", "updated_at"])
    return chat


def switch_branch(chat: AgentChat, message_id) -> AgentChat:
    """Show the branch through ``message_id``: its newest leaf becomes current.

    Refused while a reply is being written in this chat (409), because the
    run moves the leaf itself when it finishes.
    """
    nodes = list(chat.messages.values_list("id", "parent_id", "created_at"))
    if not any(node_id == message_id for node_id, _, _ in nodes):
        raise ResourceNotFound("Message not found.")
    active = chat.runs.filter(status__in=ACTIVE_RUN_STATUSES).values_list("id", flat=True).first()
    if active:
        raise RunInProgress(run_id=active)
    chat.current_leaf_id = _newest_leaf(message_id, _children_by_parent(nodes))
    chat.save(update_fields=["current_leaf", "updated_at"])
    return chat


def delete_chat(chat: AgentChat) -> None:
    """Hard delete. Artifacts made in the chat stay (source_chat -> null).

    Refused while a run is active: stop it first.
    """
    active = chat.runs.filter(status__in=ACTIVE_RUN_STATUSES).values_list("id", flat=True).first()
    if active:
        raise RunInProgress(run_id=active)
    chat.delete()


def purge_expired_chats(now=None) -> int:
    """Delete chats idle for longer than AGENT_CHAT_RETENTION_DAYS (0 keeps
    every chat). A chat with a run in progress stays; artifacts and reports
    made in a deleted chat stay too (their chat link becomes null).
    Returns how many chats were deleted."""
    from apps.agent.conf import agent_conf

    days = agent_conf().chat_retention_days
    if days <= 0:
        return 0
    cutoff = (now or timezone.now()) - timedelta(days=days)
    busy = AgentRun.objects.filter(status__in=ACTIVE_RUN_STATUSES).values("chat_id")
    expired = list(
        AgentChat.objects.annotate(activity_at=Coalesce("last_message_at", "created_at"))
        .filter(activity_at__lt=cutoff)
        .exclude(id__in=busy)
        .values_list("id", flat=True)
    )
    deleted = 0
    for start in range(0, len(expired), PURGE_BATCH):
        _, per_model = AgentChat.objects.filter(
            id__in=expired[start:start + PURGE_BATCH],
        ).delete()
        deleted += per_model.get(AgentChat._meta.label, 0)
    return deleted


def set_feedback(user, website, message_id, *, value: int, reason: str = "", comment: str = "") -> int:
    """Thumbs up (1), down (-1) or clear (0) on one of the user's replies."""
    message = (
        AgentMessage.objects.filter(
            pk=message_id, chat__user=user, chat__website=website,
        ).first()
    )
    if message is None:
        raise ResourceNotFound("Message not found.")
    if message.role != MessageRole.ASSISTANT:
        raise CanseeException(
            "Feedback is for the agent's replies.", code="invalid", status_code=400,
        )
    if value in (FEEDBACK_UP, FEEDBACK_DOWN):
        message.feedback = value
        message.feedback_reason = reason
        message.feedback_comment = comment
        message.feedback_at = timezone.now()
    else:
        message.feedback = None
        message.feedback_reason = ""
        message.feedback_comment = ""
        message.feedback_at = None
    message.save(update_fields=["feedback", "feedback_reason", "feedback_comment", "feedback_at"])
    return value


# ── Stale runs ────────────────────────────────────────────────────────────


def _sweep_stale_runs(chat_id) -> None:
    """End this chat's runs whose process stopped heartbeating (one query
    when there are none)."""
    from apps.agent.runtime.sweeper import sweep_chat

    sweep_chat(chat_id)


# ── Cursor ────────────────────────────────────────────────────────────────


def _encode_cursor(at: datetime, chat_id) -> str:
    raw = json.dumps([at.isoformat(), str(chat_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at_text, id_text = json.loads(raw)
        at = parse_datetime(at_text)
        chat_id = uuid.UUID(id_text)
    except (binascii.Error, ValueError, TypeError, AttributeError, UnicodeDecodeError):
        raise InvalidCursor() from None
    if at is None or timezone.is_naive(at):
        raise InvalidCursor()
    return at, chat_id
