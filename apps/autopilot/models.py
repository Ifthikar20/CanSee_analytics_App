"""Autopilot: a project's daily run and its text (wave 4 contract 3 and 5).

``Autopilot`` holds one project's settings and the consent record of its
daily text. ``AutopilotRun`` is one run, at most one per project per local
day (``autopilot_run_one_per_day``), with its counters, its cost and the
text it built. ``AutopilotRunPrompt`` is one tracked prompt of a run: its
engine answers (asked through ``prompt_crawler.EngineScan``, whose audit it
points to) and its Google page one check.

Counters on a run are only ever updated with F() expressions or recounted
from its prompt rows, so workers on several threads never lose an update.
"""

import secrets
import uuid
from datetime import time

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin

CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCES = (CADENCE_DAILY, CADENCE_WEEKLY)

# The text's link: {FRONTEND_URL}/r/{code} (contract 5a).
LINK_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
LINK_CODE_LENGTH = 8


def default_google_cadence() -> str:
    """The cadence a new project starts with (AUTOPILOT_GOOGLE_CADENCE).

    A callable, so the migration keeps a reference and changing the setting
    needs no new migration."""
    value = getattr(settings, "AUTOPILOT_GOOGLE_CADENCE", CADENCE_WEEKLY)
    return value if value in CADENCES else CADENCE_WEEKLY


def new_link_code() -> str:
    return "".join(secrets.choice(LINK_ALPHABET) for _ in range(LINK_CODE_LENGTH))


class Autopilot(TimestampMixin):
    """One project's Autopilot settings (contract 3a) and the consent record
    of its daily text (contract 3b).

    The text goes only to ``sms_subscription`` (a number its owner verified
    and agreed for), and only while ``sms_consented_at`` is not older than
    the number's own ``consented_at``: a number verified again needs a new
    agreement.
    """

    PAUSED_FAILURES = "failures"
    CONSENT_VERSION = "digest-v1"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.OneToOneField(
        "websites.Website",
        related_name="autopilot",
        on_delete=models.CASCADE,
    )
    enabled = models.BooleanField(default=False, db_index=True)
    # Local wall-clock time in ``timezone``, on the quarter hour.
    run_time = models.TimeField(default=time(7, 0))
    timezone = models.CharField(max_length=64, default="UTC")
    run_engines = models.BooleanField(default=True)
    run_google = models.BooleanField(default=True)
    run_analysis = models.BooleanField(default=True)
    google_cadence = models.CharField(max_length=8, default=default_google_cadence)

    # -- the daily text ------------------------------------------------------
    sms_digest = models.BooleanField(default=False)
    sms_subscription = models.ForeignKey(
        "notifications.SmsSubscription",
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Consent record (contract 3b): when, from where, which wording.
    sms_consented_at = models.DateTimeField(null=True, blank=True)
    sms_consent_ip = models.GenericIPAddressField(null=True, blank=True)
    sms_consent_version = models.CharField(max_length=16, blank=True)
    # The opt-in text's (template F) last send; cleared when the recipient changes.
    sms_optin_sent_at = models.DateTimeField(null=True, blank=True)
    quiet_start = models.TimeField(default=time(21, 0))
    quiet_end = models.TimeField(default=time(8, 0))

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Where the next run starts when the allowance fits only part of the
    # tracked prompts, so a partial day does not always skip the same ones.
    prompt_offset = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    # "" or "failures" (paused after 3 failed runs; enabled is then off).
    paused_reason = models.CharField(max_length=16, blank=True, default="")
    # The billing period (Period.key) whose allowance text (template D) went.
    allowance_notice_period = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "autopilot_settings"

    def __str__(self):
        return f"Autopilot({self.website_id}, enabled={self.enabled})"


class AutopilotRun(TimestampMixin):
    """One Autopilot run of a project (contract 3c RunSummary)."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"
    STATUS_INTERRUPTED = "interrupted"
    ACTIVE = (STATUS_QUEUED, STATUS_RUNNING)
    TERMINAL = (
        STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED,
        STATUS_INTERRUPTED,
    )

    STAGE_PLAN = "plan"
    STAGE_ENGINES = "engines"
    STAGE_GOOGLE = "google"
    STAGE_ANALYSIS = "analysis"
    STAGE_FINALIZE = "finalize"
    STAGE_FINISHED = "finished"

    TRIGGER_SCHEDULE = "schedule"
    TRIGGER_MANUAL = "manual"

    TEXT_NOT_REQUESTED = "not_requested"
    TEXT_WAITING = "waiting"
    TEXT_SENDING = "sending"
    TEXT_SENT = "sent"
    TEXT_FAILED = "failed"
    TEXT_SKIPPED = "skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="autopilot_runs",
        on_delete=models.CASCADE,
    )
    # The project's local date when the run was created.
    local_date = models.DateField()
    trigger = models.CharField(max_length=12, default=TRIGGER_SCHEDULE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=12, default=STATUS_QUEUED, db_index=True)
    stage = models.CharField(max_length=12, default=STAGE_PLAN)
    # What the plan step decided: engines, Google, allowance, the confirm
    # of a manual run.
    plan = models.JSONField(default=dict, blank=True)

    prompts_planned = models.PositiveIntegerField(default=0)
    prompts_done = models.PositiveIntegerField(default=0)
    prompts_failed = models.PositiveIntegerField(default=0)
    prompts_skipped = models.PositiveIntegerField(default=0)
    over_limit = models.PositiveIntegerField(default=0)
    answers_asked = models.PositiveIntegerField(default=0)
    answers_answered = models.PositiveIntegerField(default=0)
    answers_named = models.PositiveIntegerField(default=0)
    # Share-of-answers change against the previous finished run, over the
    # (prompt, engine) pairs answered in both; None when not measurable.
    change_pts = models.FloatField(null=True, blank=True)

    # "" until the Google step, then done / partial / off / skipped / failed.
    google_status = models.CharField(max_length=12, blank=True)
    google_measured = models.PositiveIntegerField(default=0)
    google_checked = models.PositiveIntegerField(default=0)
    google_reused = models.PositiveIntegerField(default=0)
    google_page_one = models.PositiveIntegerField(default=0)
    # Google results pages the provider billed.
    google_pages = models.PositiveIntegerField(default=0)

    analysis_run = models.ForeignKey(
        "insights.InsightRun",
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # "" until the analysis step, then done / running / off / skipped /
    # failed / pending.
    analysis_status = models.CharField(max_length=12, blank=True)
    new_contradicted = models.PositiveIntegerField(null=True, blank=True)

    # Written at finalize from the usage ledger (contract 7).
    ai_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    # Public text only.
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    # Times the dispatcher handed a silent stage to a new worker.
    resumes = models.PositiveSmallIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)

    link_code = models.CharField(max_length=12, unique=True, default=new_link_code)
    text_status = models.CharField(max_length=16, default=TEXT_NOT_REQUESTED)
    text_reason = models.CharField(max_length=24, blank=True)
    text_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    text_sent_at = models.DateTimeField(null=True, blank=True)
    # The text as built (it holds no phone number); A to E.
    text_body = models.TextField(blank=True)
    text_template = models.CharField(max_length=2, blank=True)
    text_segments = models.PositiveSmallIntegerField(default=0)
    text_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "autopilot_run"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["website", "local_date"], name="autopilot_run_one_per_day",
            ),
        ]
        indexes = [
            models.Index(fields=["website", "-created_at"]),
            models.Index(fields=["status", "stage"]),
            models.Index(fields=["text_status", "text_due_at"]),
        ]

    def __str__(self):
        return f"AutopilotRun({self.website_id}, {self.local_date}, {self.status})"


class AutopilotRunPrompt(models.Model):
    """One tracked prompt of a run: its engine answers and its Google check."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"

    GOOGLE_PENDING = ""
    GOOGLE_RUNNING = "running"
    GOOGLE_CHECKED = "checked"
    GOOGLE_REUSED = "reused"
    GOOGLE_SKIPPED = "skipped"
    GOOGLE_FAILED = "failed"
    GOOGLE_OFF = "off"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AutopilotRun, related_name="prompt_rows", on_delete=models.CASCADE)
    brand_prompt = models.ForeignKey(
        "prompt_library.BrandPrompt",
        related_name="+",
        on_delete=models.CASCADE,
    )
    # 0-based place in the run.
    position = models.PositiveIntegerField(default=0)
    # The engines step: queued, running, done (one answer at least), failed, skipped.
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    # The scan's audit, set as soon as its first ask returns, so
    # org_entitlements.prompts_used_this_month can leave it out.
    audit = models.ForeignKey(
        "llm_ranking.LLMRankingAudit",
        related_name="autopilot_rows",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # {"claude": {"asked": 1, "answered": 1, "named": 1, "rank": 2}} by public engine key.
    engines = models.JSONField(default=dict, blank=True)
    answers_asked = models.PositiveSmallIntegerField(default=0)
    answers_answered = models.PositiveSmallIntegerField(default=0)
    answers_named = models.PositiveSmallIntegerField(default=0)
    google_check = models.ForeignKey(
        "search_gap.GoogleRankCheck",
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # "" (not yet), running, checked, reused, skipped, failed, off.
    google_status = models.CharField(max_length=10, blank=True)
    # Google results pages billed for this prompt (a retry included).
    google_pages = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=32, blank=True)
    # Claims of the row by each step; a row a crashed worker left running is
    # given back once, then ends failed.
    attempts = models.PositiveSmallIntegerField(default=0)
    google_attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    google_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "autopilot_runprompt"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "brand_prompt"], name="autopilot_runprompt_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["run", "status"]),
            models.Index(fields=["run", "google_status"]),
        ]

    def __str__(self):
        return f"AutopilotRunPrompt({self.run_id}, {self.position}, {self.status})"
