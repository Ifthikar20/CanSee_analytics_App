"""Ask AI (the prompt box): one buyer question asked several times per engine.

A run is estimated and confirmed first, as country checks are. Its answers
stay in these tables: they never write LLMRankingResult rows, which every
tracked number reads, so asking a question never changes a tracked number.
The published retention for LLM answers applies to them
(apps.analytics.tasks.prune_llm_results).
"""

import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class PromptBoxRun(TimestampMixin):
    """One question, asked ``samples`` times to each engine in ``engines``."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_INTERRUPTED = "interrupted"
    ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
    TERMINAL_STATUSES = (
        STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED,
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="prompt_box_runs",
        on_delete=models.CASCADE,
    )
    # Spend is attributed to this user.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # The question, whitespace collapsed to one line.
    text = models.CharField(max_length=300)
    # Answers asked of each engine.
    samples = models.PositiveSmallIntegerField()
    # Public engine keys, display order.
    engines = models.JSONField(default=list)
    status = models.CharField(max_length=12, default=STATUS_QUEUED, db_index=True)
    # The estimate at creation, and {answers, ai_usd_max} the user confirmed.
    estimate = models.JSONField(default=dict)
    confirmed = models.JSONField(default=dict)
    # Written when the run ends, from its answer rows.
    answers_total = models.PositiveIntegerField(default=0)
    answers_done = models.PositiveIntegerField(default=0)
    answers_failed = models.PositiveIntegerField(default=0)
    # Written when the run ends, from the usage ledger rows tagged with it.
    ai_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    cancel_requested = models.BooleanField(default=False)
    # Public text only.
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "prompt_box_run"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["website", "-created_at"])]

    def __str__(self):
        return f"PromptBoxRun({self.website_id}, {self.status})"

    @property
    def audit_id(self) -> str:
        """The usage ledger tag of this run's answers and extractions."""
        return f"prompt-box:{self.id}"


class PromptBoxAnswer(models.Model):
    """One answer: sample ``sample`` (1-based) of ``engine`` (a public key)."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(PromptBoxRun, related_name="answers", on_delete=models.CASCADE)
    engine = models.CharField(max_length=16)
    # The backend provider key (PROVIDERS) and the model that answered.
    provider = models.CharField(max_length=16, blank=True)
    model_id = models.CharField(max_length=64, blank=True)
    sample = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    # Whether the answer named the brand, where, and in what tone. None until
    # an answer with text was read: an empty answer is never "not named".
    mentioned = models.BooleanField(null=True, blank=True)
    rank = models.PositiveSmallIntegerField(null=True, blank=True)
    sentiment = models.CharField(max_length=16, blank=True)
    # [{name, position}] and source URLs, at most 10 each.
    brands = models.JSONField(default=list, blank=True)
    citations = models.JSONField(default=list, blank=True)
    # Where the answer names the brand (the extraction's context), else its
    # opening.
    excerpt = models.CharField(max_length=300, blank=True)
    answer_text = models.TextField(blank=True)
    # The web searches the engine reported; None when it does not report them.
    search_queries = models.JSONField(null=True, blank=True)
    web_searches = models.PositiveSmallIntegerField(null=True, blank=True)
    # "heuristic" when the structured extraction fell back.
    extraction_model = models.CharField(max_length=100, blank=True)
    # Public text only.
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "prompt_box_answer"
        constraints = [
            models.UniqueConstraint(
                fields=["run", "engine", "sample"], name="prompt_box_answer_unique",
            ),
        ]

    def __str__(self):
        return f"PromptBoxAnswer({self.engine} #{self.sample}, {self.status})"
