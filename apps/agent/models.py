"""Persistence for the agent workspace (design/agent_backend.md section 3).

Chats and messages are private to the user who started them; artifacts,
reports and brand context are shared with every member of the website.
Postgres is the source of truth for a run: the event stream is rebuilt
from AgentMessage.parts / parts_seq after a reload or a process restart.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    ARTIFACT_SUMMARY_MAX_LENGTH,
    ARTIFACT_TITLE_MAX_LENGTH,
    CHAT_PREVIEW_MAX_LENGTH,
    CHAT_TITLE_MAX_LENGTH,
    DEFAULT_ENTRY_AGENT,
    ArtifactContentType,
    ArtifactKind,
    MessageRole,
    MessageStatus,
    RunStatus,
    RunTrigger,
    TitleSource,
    VersionSource,
)


class AgentPromptSnapshot(models.Model):
    """An immutable, content-addressed system prompt plus tool set.

    ``id`` is the sha256 of the canonical JSON of {agent_key, version,
    system, tools, config}. A chat pins the snapshot it started on, so its
    history replays against byte-identical system and tools.
    """

    id = models.CharField(max_length=64, primary_key=True)
    agent_key = models.CharField(max_length=40)
    version = models.CharField(max_length=40)
    system_text = models.TextField()
    # Canonical json.dumps(sort_keys=True, separators=(",", ":"),
    # ensure_ascii=False) of the tools, sorted by name.
    tools_json = models.TextField(default="[]")
    # {model, effort, thinking, max_tokens, cache_ttl}
    config = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agent_prompt_snapshot"

    def __str__(self):
        return f"AgentPromptSnapshot({self.agent_key} v{self.version}, {self.id[:12]})"


class AgentChat(models.Model):
    """One chat thread of one user in one website. Private to that user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website", related_name="agent_chats", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="agent_chats", on_delete=models.CASCADE
    )
    snapshot = models.ForeignKey(
        AgentPromptSnapshot, related_name="chats", on_delete=models.PROTECT
    )
    entry_agent = models.CharField(max_length=40, default=DEFAULT_ENTRY_AGENT)
    title = models.CharField(max_length=CHAT_TITLE_MAX_LENGTH, blank=True)
    title_source = models.CharField(
        max_length=8, choices=TitleSource.choices, default=TitleSource.AUTO
    )
    preview = models.CharField(max_length=CHAT_PREVIEW_MAX_LENGTH, blank=True)
    # The visible branch ends here; the path back to the root is what the
    # chat shows and what the next turn replays.
    current_leaf = models.ForeignKey(
        "AgentMessage",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_chat"
        indexes = [
            models.Index(fields=["website", "user", "-last_message_at"]),
            models.Index(fields=["user", "-last_message_at"]),
        ]

    def __str__(self):
        # Ids only: the title is derived from the user's own words.
        return f"AgentChat({self.id})"


class AgentMessage(models.Model):
    """A node in a chat's message tree.

    ``wire`` holds the exact API wire messages this node contributes, in
    order, as JSON text: TEXT rather than JSONB because JSONB rejects
    \\u0000, and replay must be byte-faithful. Always json.dumps/json.loads
    it; never rebuild it from ``parts``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(AgentChat, related_name="messages", on_delete=models.CASCADE)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.CASCADE,
    )
    role = models.CharField(max_length=16, choices=MessageRole.choices)
    depth = models.PositiveIntegerField(default=0)
    wire = models.TextField(default="[]")
    # UI parts (contract section 3), NUL-stripped.
    parts = models.JSONField(default=list, blank=True)
    parts_seq = models.BigIntegerField(default=0)
    # {"serp": {capture_id: SerpCapture}, "pages": {capture_id: PageCapture
    # minus reader}, "gaps": [...]}: rebuilds browser tabs after a reload.
    research = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=MessageStatus.choices, default=MessageStatus.COMPLETE
    )
    stop_reason = models.CharField(max_length=40, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    model = models.CharField(max_length=80, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    feedback = models.SmallIntegerField(null=True, blank=True)
    feedback_reason = models.CharField(max_length=64, blank=True)
    feedback_comment = models.TextField(blank=True)
    feedback_at = models.DateTimeField(null=True, blank=True)
    saved_report = models.ForeignKey(
        "AgentArtifact",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "agent_message"
        indexes = [
            models.Index(fields=["chat", "created_at"]),
        ]

    def __str__(self):
        return f"AgentMessage({self.role}, {self.id})"


class AgentRun(models.Model):
    """One execution of one turn: what runs, where, and what it cost."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(AgentChat, related_name="runs", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="agent_runs", on_delete=models.CASCADE
    )
    website = models.ForeignKey(
        "websites.Website", related_name="agent_runs", on_delete=models.CASCADE
    )
    user_message = models.ForeignKey(AgentMessage, related_name="+", on_delete=models.CASCADE)
    assistant_message = models.OneToOneField(
        AgentMessage, related_name="run", on_delete=models.CASCADE
    )
    trigger = models.CharField(max_length=16, choices=RunTrigger.choices, default=RunTrigger.SEND)
    status = models.CharField(max_length=16, choices=RunStatus.choices, default=RunStatus.QUEUED)
    stop_reason = models.CharField(max_length=40, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    # "<hostname>:<pid>:<boot_ts>" of the process executing the run.
    executor_id = models.CharField(max_length=80, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    last_seq = models.BigIntegerField(default=0)
    requests = models.PositiveIntegerField(default=0)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cache_read_tokens = models.BigIntegerField(default=0)
    cache_write_tokens = models.BigIntegerField(default=0)
    web_search_requests = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    # True when a request ended before its final usage arrived (cancel,
    # stream error) and the tokens were estimated.
    usage_estimated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "agent_run"
        constraints = [
            models.UniqueConstraint(
                fields=["chat"],
                condition=Q(status__in=ACTIVE_RUN_STATUSES),
                name="agent_run_one_active_per_chat",
            ),
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="agent_run_idem",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "heartbeat_at"]),
            models.Index(fields=["chat", "-created_at"]),
        ]

    def __str__(self):
        return f"AgentRun({self.id}, {self.status})"


class AgentArtifact(models.Model):
    """A page, document, sheet or report. Shared with the website's members."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website", related_name="agent_artifacts", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=16, choices=ArtifactKind.choices)
    agent_key = models.CharField(max_length=40, blank=True)
    title = models.CharField(max_length=ARTIFACT_TITLE_MAX_LENGTH)
    latest_version = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    source_chat = models.ForeignKey(
        AgentChat,
        null=True,
        blank=True,
        related_name="artifacts",
        on_delete=models.SET_NULL,
    )
    source_message = models.ForeignKey(
        AgentMessage,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # Soft delete: set = gone from every listing.
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_artifact"
        indexes = [
            models.Index(fields=["website", "deleted_at", "-updated_at"]),
            models.Index(fields=["website", "kind", "-updated_at"]),
        ]

    def __str__(self):
        return f"AgentArtifact({self.kind}, {self.id})"


class AgentArtifactVersion(models.Model):
    """One immutable version of an artifact's content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artifact = models.ForeignKey(
        AgentArtifact, related_name="versions", on_delete=models.CASCADE
    )
    version = models.PositiveIntegerField()
    content = models.TextField()
    content_type = models.CharField(max_length=32, choices=ArtifactContentType.choices)
    # Reports: the research snapshot {serp, pages, engines, gaps}.
    data = models.JSONField(null=True, blank=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField(default=0)
    summary = models.CharField(max_length=ARTIFACT_SUMMARY_MAX_LENGTH, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    source = models.CharField(
        max_length=16, choices=VersionSource.choices, default=VersionSource.AGENT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    run = models.ForeignKey(
        AgentRun,
        null=True,
        blank=True,
        related_name="artifact_versions",
        on_delete=models.SET_NULL,
    )
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agent_artifact_version"
        constraints = [
            models.UniqueConstraint(
                fields=["artifact", "version"], name="agent_artifact_version_unique"
            ),
        ]

    def __str__(self):
        return f"AgentArtifactVersion({self.artifact_id}, v{self.version})"


class AgentBrandContext(models.Model):
    """Per-website brand notes the agents write with. Optimistic ``revision``."""

    website = models.OneToOneField(
        "websites.Website",
        primary_key=True,
        related_name="agent_brand_context",
        on_delete=models.CASCADE,
    )
    audience = models.TextField(blank=True)
    voice = models.TextField(blank=True)
    # [{name, description, url}]
    offerings = models.JSONField(default=list, blank=True)
    # [str]
    rules = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_brand_context"

    def __str__(self):
        return f"AgentBrandContext({self.website_id}, r{self.revision})"
