"""Read-only operator views of runs and artifacts.

Chat content (titles, previews, messages, wire, parts), artifact bodies
and research plans, tables and findings are never shown: the admin lists
ids, owners, states, timings and cost.
"""
from django.contrib import admin

from apps.agent.models import AgentArtifact, AgentRun, AgentSwarm, AgentSwarmVersion


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
