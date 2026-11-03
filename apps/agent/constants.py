"""Shared vocabulary of the agent workspace: models, runtime and API.

String values are part of the backend/frontend contract
(design/agent_contract.md); never rename one without the frontend.
"""
from django.db import models

# AITokenUsage.module for every agent request.
USAGE_MODULE = "agent"

# Envelope version carried by every run event ({v, seq, type, ...}).
EVENT_VERSION = 1

# Field sizes shared by the models and the serializers.
CHAT_TITLE_MAX_LENGTH = 120
CHAT_PREVIEW_MAX_LENGTH = 200
ARTIFACT_TITLE_MAX_LENGTH = 200
ARTIFACT_SUMMARY_MAX_LENGTH = 500
# Generated chat titles (model or first-message fallback).
GENERATED_TITLE_MAX_CHARS = 60


class AgentKey(models.TextChoices):
    GAP_ANALYST = "gap_analyst", "Gap analyst"
    LANDING_PAGE = "landing_page", "Landing page"
    AEO_ARTICLE = "aeo_article", "AEO article"
    AD_CAMPAIGN = "ad_campaign", "Ad campaign"


DEFAULT_ENTRY_AGENT = AgentKey.GAP_ANALYST.value
# Agents that run_agent dispatches; each writes one artifact.
SUBAGENT_KEYS = (
    AgentKey.LANDING_PAGE.value,
    AgentKey.AEO_ARTICLE.value,
    AgentKey.AD_CAMPAIGN.value,
)


class TitleSource(models.TextChoices):
    AUTO = "auto", "First message"
    MODEL = "model", "Generated"
    USER = "user", "Set by user"


class MessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class MessageStatus(models.TextChoices):
    COMPLETE = "complete", "Complete"
    STREAMING = "streaming", "Streaming"
    TRUNCATED = "truncated", "Truncated"
    REFUSED = "refused", "Refused"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"


class RunTrigger(models.TextChoices):
    SEND = "send", "Send"
    REGENERATE = "regenerate", "Regenerate"
    EDIT = "edit", "Edit"
    CONTINUE = "continue", "Continue"
    # A research re-run turn: it runs the research agents and makes no
    # model request of its own.
    RERUN = "rerun", "Re-run"


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    INTERRUPTED = "interrupted", "Interrupted"


# A chat holds at most one run in these states (agent_run_one_active_per_chat).
ACTIVE_RUN_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
TERMINAL_RUN_STATUSES = (
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.INTERRUPTED.value,
)


class ArtifactKind(models.TextChoices):
    PAGE = "page", "Landing page"
    DOCUMENT = "document", "Document"
    SHEET = "sheet", "Sheet"
    REPORT = "report", "Report"


class ArtifactContentType(models.TextChoices):
    HTML = "text/html", "HTML"
    MARKDOWN = "text/markdown", "Markdown"
    JSON = "application/json", "JSON"


CONTENT_TYPE_BY_KIND = {
    ArtifactKind.PAGE.value: ArtifactContentType.HTML.value,
    ArtifactKind.DOCUMENT.value: ArtifactContentType.MARKDOWN.value,
    ArtifactKind.REPORT.value: ArtifactContentType.MARKDOWN.value,
    ArtifactKind.SHEET.value: ArtifactContentType.JSON.value,
}

# The agents/ catalog's output_kind.
OUTPUT_KIND_BY_AGENT = {
    AgentKey.GAP_ANALYST.value: ArtifactKind.REPORT.value,
    AgentKey.LANDING_PAGE.value: ArtifactKind.PAGE.value,
    AgentKey.AEO_ARTICLE.value: ArtifactKind.DOCUMENT.value,
    AgentKey.AD_CAMPAIGN.value: ArtifactKind.SHEET.value,
}


class VersionSource(models.TextChoices):
    AGENT = "agent", "Agent"
    USER = "user", "User"
    RESTORE = "restore", "Restore"


# AgentMessage.feedback values; 0 from the API clears the field.
FEEDBACK_UP = 1
FEEDBACK_DOWN = -1


class PartType:
    """UI part types inside AgentMessage.parts (contract section 3)."""

    THINKING = "thinking"
    TEXT = "text"
    TOOL = "tool"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    SERP = "serp"
    PAGE_INSPECT = "page_inspect"
    ENGINE_CHECK = "engine_check"
    AGENT_CALL = "agent_call"
    CHART = "chart"
    BROWSER_OPEN = "browser_open"
    NOTICE = "notice"
    # The research agents (design/swarm_contract.md section 5).
    SWARM = "swarm"
    SWARM_TABLE = "swarm_table"


class EventType:
    """Run event types shared by SSE and long-poll (contract section 2)."""

    RUN_STARTED = "run.started"
    RUN_RETRYING = "run.retrying"
    RUN_USAGE = "run.usage"
    CHAT_UPDATED = "chat.updated"
    TURN_RESET = "turn.reset"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"
    RESYNC = "resync"
    PART_ADDED = "part.added"
    PART_DELTA = "part.delta"
    PART_UPDATED = "part.updated"
    ARTIFACT_STARTED = "artifact.started"
    ARTIFACT_DELTA = "artifact.delta"
    ARTIFACT_SNAPSHOT = "artifact.snapshot"
    ARTIFACT_COMPLETED = "artifact.completed"
    ARTIFACT_FAILED = "artifact.failed"
    BROWSER_OPEN = "browser.open"
    BROWSER_UPDATE = "browser.update"
    # One research agent of a swarm part (swarm contract section 4).
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_FINISHED = "task.finished"
    # A capture a research agent took, for the message's research store.
    RESEARCH_RECORDED = "research.recorded"


TERMINAL_EVENT_TYPES = (
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.RUN_INTERRUPTED,
)

# Part events that patch one task of a swarm part.
TASK_EVENT_TYPES = (EventType.TASK_STARTED, EventType.TASK_PROGRESS, EventType.TASK_FINISHED)


# ── Research agents (design/swarm_contract.md) ──────────────────────────


class SwarmCategory(models.TextChoices):
    """The fixed categories of a swarm, in display order."""

    GOOGLE = "google", "Google (SEO)"
    ENGINES = "engines", "AI engines (GEO)"
    COMPETITORS = "competitors", "Competitor pages"
    SITE = "site", "Your site and content gaps"
    SOURCES = "sources", "Sources and citations"


class WorkerType:
    """What one task does (contract section 1c)."""

    SERP_PAGE = "serp_page"
    ENGINE_ANSWER = "engine_answer"
    PAGE_INSPECT = "page_inspect"
    CONTENT_GAP = "content_gap"
    CITATIONS = "citations"


class TaskStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    STOPPED = "stopped", "Stopped"


class SwarmStatus(models.TextChoices):
    """The status of a ``swarm`` part."""

    PLANNING = "planning", "Planning"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    PARTIAL = "partial", "Partial"
    ERROR = "error", "Error"
    CANCELLED = "cancelled", "Cancelled"
    INTERRUPTED = "interrupted", "Interrupted"


class VersionStatus(models.TextChoices):
    """AgentSwarmVersion.status: one execution of a swarm."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    PARTIAL = "partial", "Partial"
    ERROR = "error", "Error"
    CANCELLED = "cancelled", "Cancelled"
    INTERRUPTED = "interrupted", "Interrupted"


class SwarmScope(models.TextChoices):
    """What one execution runs: every task, one category or one task."""

    ALL = "all", "All agents"
    CATEGORY = "category", "One category"
    TASK = "task", "One agent"


class FindingStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    DISMISSED = "dismissed", "Dismissed"


class FindingSeverity(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


# A task in one of these never changes again within its part.
TERMINAL_TASK_STATUSES = (
    TaskStatus.DONE.value,
    TaskStatus.FAILED.value,
    TaskStatus.SKIPPED.value,
    TaskStatus.STOPPED.value,
)
LIVE_SWARM_STATUSES = (SwarmStatus.PLANNING.value, SwarmStatus.RUNNING.value)
# Version statuses of an execution that has not ended.
ACTIVE_VERSION_STATUSES = (VersionStatus.QUEUED.value, VersionStatus.RUNNING.value)
FINDING_CATEGORIES = (*SwarmCategory.values, "other")
FINDING_CATEGORY_LABELS = {**dict(SwarmCategory.choices), "other": "Other"}
MAX_SWARM_VERSIONS = 50
SWARM_TITLE_MAX_LENGTH = 120
# The ``engines`` entry of a cited source that Google's AI Overview cited
# (the citations agent counts its references next to the engines' citations).
AI_OVERVIEW_SOURCE = "ai_overview"
AI_OVERVIEW_SOURCE_LABEL = "Google AI Overview"
