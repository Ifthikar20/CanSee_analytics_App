"""Request validation and response shapes of the agent API.

Response shapes follow design/agent_contract.md section 4, and
design/swarm_contract.md section 6d for the research agents and findings.
Output is JSON-native (strings, numbers, lists, dicts) so it can go into
an event payload as well as a response. The runtime's live views import
MessageSerializer and RunSerializer from here.
"""
from __future__ import annotations

import uuid

from rest_framework import serializers

from apps.agent.constants import (
    ARTIFACT_TITLE_MAX_LENGTH,
    CHAT_TITLE_MAX_LENGTH,
    DEFAULT_ENTRY_AGENT,
    FEEDBACK_DOWN,
    FEEDBACK_UP,
    FINDING_CATEGORIES,
    FINDING_CATEGORY_LABELS,
    TERMINAL_TASK_STATUSES,
    AgentKey,
    FindingSeverity,
    FindingStatus,
    MessageRole,
    RunStatus,
    SwarmCategory,
    SwarmScope,
)
from apps.agent.services.findings import (
    DETAIL_MAX_LENGTH,
    MAX_ROWS_PER_CREATE,
    PROMPT_TEXT_MAX_LENGTH,
    SEARCH_MAX_LENGTH,
    SHEETS,
    STATUS_FILTER_ALL,
    TITLE_MAX_LENGTH,
)

_datetime_field = serializers.DateTimeField()


def dt(value) -> str | None:
    return _datetime_field.to_representation(value) if value else None


def str_id(value) -> str | None:
    return str(value) if value else None


def display_name(user) -> str | None:
    if user is None:
        return None
    return (getattr(user, "full_name", "") or "").strip() or user.email


# ── Messages, runs, chats ─────────────────────────────────────────────────


class MessageSerializer(serializers.BaseSerializer):
    """Contract ``Message``, plus ``research``: the SERP and page captures
    behind its browser tabs, so the pane can rebuild them after a reload.

    Pass ``context={"sibling_ids": {message_id: [ids]}}`` when rendering a
    whole path; without it each message costs one query for its siblings.
    """

    def to_representation(self, message):
        siblings = (self.context.get("sibling_ids") or {}).get(message.id)
        if siblings is None:
            from apps.agent.services.chats import sibling_ids_for

            siblings = sibling_ids_for(message)
        run = getattr(message, "run", None) if message.role == MessageRole.ASSISTANT else None
        duration_ms = None
        if message.started_at and message.finished_at:
            duration_ms = max(
                0, int((message.finished_at - message.started_at).total_seconds() * 1000),
            )
        return {
            "id": str(message.id),
            "chat_id": str(message.chat_id),
            "parent_id": str_id(message.parent_id),
            "role": message.role,
            "status": message.status,
            "parts": message.parts or [],
            "parts_seq": message.parts_seq,
            "created_at": dt(message.created_at),
            "started_at": dt(message.started_at),
            "finished_at": dt(message.finished_at),
            "duration_ms": duration_ms,
            "model": message.model or None,
            "error_code": message.error_code or None,
            "error_message": message.error_message or None,
            "feedback": message.feedback,
            "sibling_ids": [str(i) for i in siblings],
            "run_id": str(run.id) if run is not None else None,
            "saved_report_id": str_id(message.saved_report_id),
            "research": message.research or {},
        }


def run_queue(run) -> dict | None:
    """Contract ``RunQueue``: {position, ahead, slots} while a queued run
    waits for a free run thread in this process, else None."""
    if run.status != RunStatus.QUEUED:
        return None
    from apps.agent.runtime.dispatch import queue_status

    return queue_status(run.id)


class RunSerializer(serializers.BaseSerializer):
    """Contract ``Run``; ``message_id`` is the assistant message it writes."""

    def to_representation(self, run):
        return {
            "id": str(run.id),
            "chat_id": str(run.chat_id),
            "message_id": str(run.assistant_message_id),
            "status": run.status,
            "last_seq": run.last_seq,
            "queue": run_queue(run),
        }


class ChatSummarySerializer(serializers.BaseSerializer):
    def to_representation(self, chat):
        return {
            "id": str(chat.id),
            "title": chat.title,
            "preview": chat.preview,
            "created_at": dt(chat.created_at),
            "last_message_at": dt(chat.last_message_at),
            "active_run_id": str_id(getattr(chat, "active_run_id", None)),
        }


def chat_detail(view) -> dict:
    """Contract ``ChatDetail`` from a services.chats.ChatView."""
    chat = view.chat
    run = view.active_run
    return {
        "id": str(chat.id),
        "title": chat.title,
        "title_source": chat.title_source,
        "entry_agent": chat.entry_agent,
        "created_at": dt(chat.created_at),
        "last_message_at": dt(chat.last_message_at),
        "current_leaf_id": str_id(view.leaf_id),
        "messages": list(
            MessageSerializer(
                view.messages, many=True, context={"sibling_ids": view.sibling_ids},
            ).data
        ),
        "active_run": (
            {
                "id": str(run.id),
                "status": run.status,
                "message_id": str(run.assistant_message_id),
                "last_seq": run.last_seq,
                "queue": run_queue(run),
            }
            if run is not None
            else None
        ),
    }


# ── Artifacts ─────────────────────────────────────────────────────────────


def _source_chat_id(artifact, user) -> str | None:
    """The chat an artifact came from, for that chat's owner only (chats are
    private, so other members get null and no "Open in chat")."""
    chat = artifact.source_chat if artifact.source_chat_id else None
    if chat is None or user is None or chat.user_id != user.id:
        return None
    return str(chat.id)


class ArtifactSummarySerializer(serializers.BaseSerializer):
    """Contract ``ArtifactSummary``; needs ``context={"user": ...}``. Lists
    select_related("review__reviewer") so ``review`` costs no query."""

    def to_representation(self, artifact):
        from apps.agent.services.reviews import summary_for

        return {
            "id": str(artifact.id),
            "kind": artifact.kind,
            "title": artifact.title,
            "agent_key": artifact.agent_key,
            "latest_version": artifact.latest_version,
            "created_at": dt(artifact.created_at),
            "updated_at": dt(artifact.updated_at),
            "created_by_name": display_name(artifact.created_by),
            "source_chat_id": _source_chat_id(artifact, self.context.get("user")),
            "review": summary_for(artifact),
        }


def artifact_detail(artifact, version, user) -> dict:
    """Contract ``ArtifactDetail``: the summary plus the latest version."""
    data = ArtifactSummarySerializer(artifact, context={"user": user}).data
    data.update({
        "version": version.version if version else 0,
        "content": version.content if version else "",
        "content_type": version.content_type if version else None,
        "data": version.data if version else None,
        "summary": version.summary if version else "",
        "warnings": (version.warnings or []) if version else [],
    })
    return data


class ArtifactVersionSummarySerializer(serializers.BaseSerializer):
    def to_representation(self, version):
        return {
            "version": version.version,
            "created_at": dt(version.created_at),
            "summary": version.summary,
            "source": version.source,
            "size_bytes": version.size_bytes,
        }


class ArtifactVersionSerializer(serializers.BaseSerializer):
    def to_representation(self, version):
        return {
            "artifact_id": str(version.artifact_id),
            "version": version.version,
            "content": version.content,
            "content_type": version.content_type,
            "data": version.data,
            "summary": version.summary,
            "warnings": version.warnings or [],
            "source": version.source,
            "size_bytes": version.size_bytes,
            "created_at": dt(version.created_at),
            "created_by_name": display_name(version.created_by),
        }


# ── Requests ──────────────────────────────────────────────────────────────


class ChatCreateSerializer(serializers.Serializer):
    entry_agent = serializers.ChoiceField(
        choices=AgentKey.values, required=False, default=DEFAULT_ENTRY_AGENT,
    )


class ChatUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=CHAT_TITLE_MAX_LENGTH, required=False)
    current_leaf_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send a title or a current_leaf_id.")
        return attrs


class FeedbackSerializer(serializers.Serializer):
    value = serializers.ChoiceField(choices=[FEEDBACK_UP, FEEDBACK_DOWN, 0])
    reason = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    comment = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ArtifactUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=ARTIFACT_TITLE_MAX_LENGTH)


class ReportCreateSerializer(serializers.Serializer):
    message_id = serializers.UUIDField()
    title = serializers.CharField(
        max_length=ARTIFACT_TITLE_MAX_LENGTH, required=False, allow_blank=True, default="",
    )


class BrowseSerializer(serializers.Serializer):
    url = serializers.CharField(max_length=2000)


# ── Research agents ───────────────────────────────────────────────────────


def _empty_counts(total: int) -> dict:
    return {"total": total, **{status: 0 for status in TERMINAL_TASK_STATUSES}}


def _scope_label(swarm, version) -> str:
    from apps.agent.runtime.swarm.plan import scope_label

    return scope_label(swarm.categories or [], swarm.tasks or [],
                       (swarm.plan or {}).get("prompts") or [], version.scope, version.scope_key)


def _version_fields(swarm, version) -> dict:
    return {
        "version": version.version,
        "scope": version.scope,
        "scope_label": _scope_label(swarm, version),
        "status": version.status,
        "counts": version.counts or _empty_counts(len(version.task_ids or [])),
        "started_at": dt(version.started_at),
        "finished_at": dt(version.finished_at),
        "duration_ms": version.duration_ms,
        "message_id": str_id(version.message_id),
    }


def swarm_detail(swarm, versions) -> dict:
    """Contract ``SwarmDetail``; ``versions`` newest first, each with a
    ``has_table`` attribute (services.swarms.list_versions)."""
    plan = swarm.plan if isinstance(swarm.plan, dict) else {}
    tasks = [t for t in swarm.tasks or [] if isinstance(t, dict)]
    return {
        "id": str(swarm.id),
        "title": swarm.title,
        "chat_id": str(swarm.chat_id),
        "message_id": str_id(swarm.message_id),
        "created_at": dt(swarm.created_at),
        "latest_version": swarm.latest_version,
        "prompts": [
            {"key": p.get("key"), "prompt_id": p.get("prompt_id"), "text": p.get("text"),
             "location": p.get("location") or ""}
            for p in plan.get("prompts") or [] if isinstance(p, dict)
        ],
        "categories": [
            {"key": c.get("key"), "label": c.get("label"),
             "task_ids": [t["id"] for t in tasks if t.get("category") == c.get("key")]}
            for c in swarm.categories or [] if isinstance(c, dict)
        ],
        "tasks": [
            {key: t.get(key) for key in ("id", "category", "subcategory", "worker", "label")}
            for t in tasks
        ],
        "versions": [
            {**_version_fields(swarm, v), "run_id": str_id(v.run_id), "has_table": _has_table(v)}
            for v in versions
        ],
    }


def _has_table(version) -> bool:
    annotated = getattr(version, "has_table", None)
    return bool(annotated) if annotated is not None else version.table is not None


def swarm_version(swarm, version) -> dict:
    """Contract ``SwarmVersion``: one execution and its table."""
    return {"swarm_id": str(swarm.id), **_version_fields(swarm, version), "table": version.table}


class SwarmRerunSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=SwarmScope.values)
    category = serializers.ChoiceField(choices=SwarmCategory.values, required=False)
    task_id = serializers.CharField(max_length=80, required=False)

    def validate(self, attrs):
        if attrs["scope"] == SwarmScope.CATEGORY and not attrs.get("category"):
            raise serializers.ValidationError({"category": ["Choose the category to re-run."]})
        if attrs["scope"] == SwarmScope.TASK and not attrs.get("task_id"):
            raise serializers.ValidationError({"task_id": ["Choose the agent to re-run."]})
        return attrs


# ── Findings ──────────────────────────────────────────────────────────────


def _finding_source(finding, user) -> dict | None:
    """Where a finding came from; null for one written by hand. The chat
    and message ids go only to that chat's owner (chats are private)."""
    if finding.source_swarm_id is None and not finding.source_sheet:
        return None
    chat = finding.source_chat if finding.source_chat_id else None
    owner = chat is not None and user is not None and chat.user_id == user.id
    swarm = finding.source_swarm if finding.source_swarm_id else None
    return {
        "swarm_id": str_id(finding.source_swarm_id),
        "swarm_title": swarm.title if swarm is not None else None,
        "version": finding.source_version,
        "sheet": finding.source_sheet,
        "row_key": finding.source_row_key,
        "task_ids": list(finding.source_task_ids or []),
        "chat_id": str(chat.id) if owner else None,
        "message_id": str_id(finding.source_message_id) if owner else None,
    }


class FindingSerializer(serializers.BaseSerializer):
    """Contract ``Finding``; needs ``context={"user": ...}``."""

    def to_representation(self, finding):
        return {
            "id": str(finding.id),
            "title": finding.title,
            "detail": finding.detail,
            "category": finding.category,
            "category_label": FINDING_CATEGORY_LABELS.get(finding.category, finding.category),
            "severity": finding.severity,
            "status": finding.status,
            "prompt_text": finding.prompt_text,
            "evidence": finding.evidence or [],
            "source": _finding_source(finding, self.context.get("user")),
            "created_at": dt(finding.created_at),
            "updated_at": dt(finding.updated_at),
            "created_by_name": display_name(finding.created_by) if finding.created_by_id else None,
            "status_changed_at": dt(finding.status_changed_at),
            "status_changed_by_name": (display_name(finding.status_changed_by)
                                       if finding.status_changed_by_id else None),
        }


class FindingListParamsSerializer(serializers.Serializer):
    """GET findings/ query parameters; a blank value means "not filtered"."""

    status = serializers.ChoiceField(choices=[*FindingStatus.values, STATUS_FILTER_ALL],
                                     required=False, allow_blank=True)
    category = serializers.ChoiceField(choices=FINDING_CATEGORIES, required=False,
                                       allow_blank=True)
    severity = serializers.ChoiceField(choices=FindingSeverity.values, required=False,
                                       allow_blank=True)
    swarm_id = serializers.CharField(required=False, allow_blank=True)
    q = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    cursor = serializers.CharField(required=False, allow_blank=True)

    def validate_swarm_id(self, value):
        if not value:
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            raise serializers.ValidationError("Not a valid research id.") from None

    def validate_q(self, value):
        return (value or "")[:SEARCH_MAX_LENGTH]

    def validate(self, attrs):
        return {
            "status": attrs.get("status") or FindingStatus.OPEN.value,
            "category": attrs.get("category") or None,
            "severity": attrs.get("severity") or None,
            "swarm_id": attrs.get("swarm_id"),
            "q": attrs.get("q") or "",
            "cursor": attrs.get("cursor") or "",
        }


class FindingsFromSwarmSerializer(serializers.Serializer):
    swarm_id = serializers.UUIDField()
    version = serializers.IntegerField(min_value=1)
    sheet = serializers.ChoiceField(choices=SHEETS)
    row_keys = serializers.ListField(
        child=serializers.CharField(max_length=80), min_length=1, max_length=MAX_ROWS_PER_CREATE,
    )


class FindingCreateSerializer(serializers.Serializer):
    """Either ``from_swarm`` (rows of a results table) or the fields of a
    finding written by hand."""

    from_swarm = FindingsFromSwarmSerializer(required=False)
    title = serializers.CharField(max_length=TITLE_MAX_LENGTH, required=False)
    detail = serializers.CharField(max_length=DETAIL_MAX_LENGTH, required=False,
                                   allow_blank=True, default="")
    category = serializers.ChoiceField(choices=FINDING_CATEGORIES, required=False)
    severity = serializers.ChoiceField(choices=FindingSeverity.values, required=False)
    prompt_text = serializers.CharField(max_length=PROMPT_TEXT_MAX_LENGTH, required=False,
                                        allow_blank=True, default="")

    MANUAL_FIELDS = ("title", "detail", "category", "severity", "prompt_text")

    def validate(self, attrs):
        given = self.initial_data if isinstance(self.initial_data, dict) else {}
        if "from_swarm" in attrs:
            if any(given.get(key) not in (None, "") for key in self.MANUAL_FIELDS):
                raise serializers.ValidationError(
                    "Send from_swarm or a finding's fields, not both.")
            return attrs
        missing = [key for key in ("title", "category", "severity") if not attrs.get(key)]
        if missing:
            raise serializers.ValidationError({key: ["This field is required."] for key in missing})
        return attrs


class FindingUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=FindingStatus.values, required=False)
    severity = serializers.ChoiceField(choices=FindingSeverity.values, required=False)
    title = serializers.CharField(max_length=TITLE_MAX_LENGTH, required=False)
    detail = serializers.CharField(max_length=DETAIL_MAX_LENGTH, required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send a status, severity, title or detail.")
        return attrs
