"""Request validation and response shapes of the agent API.

Response shapes follow design/agent_contract.md section 4. Output is
JSON-native (strings, numbers, lists, dicts) so it can go into an event
payload as well as a response. The runtime's live views import
MessageSerializer and RunSerializer from here.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from rest_framework import serializers

from apps.agent.constants import (
    ARTIFACT_TITLE_MAX_LENGTH,
    CHAT_TITLE_MAX_LENGTH,
    DEFAULT_ENTRY_AGENT,
    FEEDBACK_DOWN,
    FEEDBACK_UP,
    AgentKey,
    MessageRole,
)
from core.validators.url_safety import safe_display_url

_datetime_field = serializers.DateTimeField()

AUDIENCE_MAX_LENGTH = 4000
VOICE_MAX_LENGTH = 2000
MAX_OFFERINGS = 20
MAX_RULES = 30
RULE_MAX_LENGTH = 500


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


class RunSerializer(serializers.BaseSerializer):
    """Contract ``Run``; ``message_id`` is the assistant message it writes."""

    def to_representation(self, run):
        return {
            "id": str(run.id),
            "chat_id": str(run.chat_id),
            "message_id": str(run.assistant_message_id),
            "status": run.status,
            "last_seq": run.last_seq,
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
    """Contract ``ArtifactSummary``; needs ``context={"user": ...}``."""

    def to_representation(self, artifact):
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


# ── Brand context ─────────────────────────────────────────────────────────


class BrandContextSerializer(serializers.BaseSerializer):
    """Contract ``BrandContext``. An unsaved context reads as revision 0."""

    def to_representation(self, context):
        return {
            "website_id": str(context.website_id),
            "audience": context.audience,
            "voice": context.voice,
            "offerings": context.offerings or [],
            "rules": context.rules or [],
            "revision": context.revision,
            "updated_at": dt(context.updated_at),
            "updated_by_name": display_name(context.updated_by) if context.updated_by_id else None,
        }


class OfferingSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_url(self, value):
        if not value:
            return ""
        if safe_display_url(value) is None or not urlsplit(value).netloc:
            raise serializers.ValidationError("Enter a full http(s) address.")
        return value


class BrandContextWriteSerializer(serializers.Serializer):
    audience = serializers.CharField(
        max_length=AUDIENCE_MAX_LENGTH, required=False, allow_blank=True, default="",
    )
    voice = serializers.CharField(
        max_length=VOICE_MAX_LENGTH, required=False, allow_blank=True, default="",
    )
    offerings = OfferingSerializer(many=True, required=False, default=list)
    rules = serializers.ListField(
        child=serializers.CharField(max_length=RULE_MAX_LENGTH, allow_blank=True),
        required=False,
        default=list,
    )
    revision = serializers.IntegerField(min_value=0)

    def validate_offerings(self, value):
        if len(value) > MAX_OFFERINGS:
            raise serializers.ValidationError(f"At most {MAX_OFFERINGS} offerings.")
        return [dict(item) for item in value]

    def validate_rules(self, value):
        rules = [rule for rule in value if rule]
        if len(rules) > MAX_RULES:
            raise serializers.ValidationError(f"At most {MAX_RULES} rules.")
        return rules


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
