"""Read-only operator views of runs, artifacts, reviews and context.

Chat content (titles, previews, messages, wire, parts), artifact bodies,
research plans, tables and findings, review notes and comments and the
text of context entries and documents are never shown: the admin lists
ids, owners, states, timings and cost.
"""
from django.contrib import admin

from apps.agent.models import (
    AgentArtifact,
    AgentArtifactReview,
    AgentContextEntry,
    AgentReviewEvent,
    AgentRun,
    AgentSwarm,
    AgentSwarmVersion,
)


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AgentRun)
class AgentRunAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "user", "website", "trigger", "status", "error_code",
        "requests", "cost_usd", "created_at", "finished_at",
    )
    list_filter = ("status", "trigger", "usage_estimated")
    list_select_related = ("user", "website")
    search_fields = ("id", "user__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = (
        "id", "user", "website", "chat_id", "user_message_id", "assistant_message_id",
        "trigger", "status", "stop_reason", "error_code", "error_message",
        "executor_id", "heartbeat_at", "cancel_requested_at", "last_seq",
        "requests", "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "web_search_requests", "cost_usd", "usage_estimated",
        "created_at", "started_at", "finished_at",
    )
    readonly_fields = fields


@admin.register(AgentArtifact)
class AgentArtifactAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "website", "kind", "agent_key", "latest_version", "created_by",
        "updated_at", "deleted_at",
    )
    list_filter = ("kind", "agent_key")
    list_select_related = ("website", "created_by")
    search_fields = ("id", "website__name")
    ordering = ("-updated_at",)
    fields = (
        "id", "website", "kind", "agent_key", "latest_version", "created_by",
        "source_chat_id", "deleted_at", "created_at", "updated_at",
    )
    readonly_fields = fields


@admin.register(AgentSwarm)
class AgentSwarmAdmin(_ReadOnlyAdmin):
    list_display = ("id", "user", "website", "latest_version", "created_at")
    list_select_related = ("user", "website")
    search_fields = ("id", "user__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = (
        "id", "user", "website", "chat_id", "message_id", "run_id", "latest_version",
        "created_at", "updated_at",
    )
    readonly_fields = fields


@admin.register(AgentSwarmVersion)
class AgentSwarmVersionAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "swarm", "version", "scope", "status", "cost_usd", "duration_ms",
    )
    list_filter = ("status", "scope")
    list_select_related = ("swarm",)
    search_fields = ("id", "swarm__id")
    ordering = ("-created_at",)
    fields = (
        "id", "swarm", "version", "scope", "status", "run_id", "message_id",
        "worker_requests", "cost_usd", "error_code", "created_at", "started_at",
        "finished_at", "duration_ms",
    )
    readonly_fields = fields


@admin.register(AgentArtifactReview)
class AgentArtifactReviewAdmin(_ReadOnlyAdmin):
    list_display = ("artifact", "website", "state", "version", "reviewer", "revision", "updated_at")
    list_filter = ("state",)
    list_select_related = ("artifact", "website", "reviewer")
    search_fields = ("artifact__id", "website__name")
    ordering = ("-updated_at",)
    fields = (
        "artifact", "website", "state", "version", "reviewer", "requested_by", "decided_by",
        "requested_at", "decided_at", "revision", "updated_at",
    )
    readonly_fields = fields


@admin.register(AgentReviewEvent)
class AgentReviewEventAdmin(_ReadOnlyAdmin):
    list_display = ("id", "artifact", "kind", "version", "actor", "recipient", "email",
                    "created_at")
    list_filter = ("kind", "email")
    list_select_related = ("artifact", "actor", "recipient")
    search_fields = ("id", "artifact__id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    fields = (
        "id", "artifact", "website", "kind", "version", "actor", "reviewer", "recipient",
        "email", "created_at",
    )
    readonly_fields = fields


@admin.register(AgentContextEntry)
class AgentContextEntryAdmin(_ReadOnlyAdmin):
    list_display = ("id", "website", "section", "status", "origin", "revision", "updated_at")
    list_filter = ("section", "status", "origin")
    list_select_related = ("website",)
    search_fields = ("id", "website__name")
    ordering = ("-updated_at",)
    fields = (
        "id", "website", "section", "status", "origin", "position", "revision",
        "build_run_id", "created_by", "updated_by", "created_at", "updated_at",
    )
    readonly_fields = fields
