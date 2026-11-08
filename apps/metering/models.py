"""
Polar.sh usage-metering plumbing.

PolarEventOutbox is a transactional outbox: a row is written in the same
database transaction as the AITokenUsage ledger insert, then flushed to
Polar's events-ingest API by a Celery task (or `manage.py polar_flush` in
dev). Polar events are immutable and deduplicated server-side on
`external_id`, so a flush retry after an ambiguous failure can never
double-count — the outbox only has to guarantee at-least-once delivery.

PolarCustomer marks which users have been provisioned as Polar customers
(external_id = str(user.id)). Phase 2 (billing cutover) reuses it.
"""
import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class PolarEventOutbox(TimestampMixin):
    """One usage event awaiting (or having completed) delivery to Polar."""

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_DEAD = "dead"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_DEAD, "Dead"),
    ]

    MAX_ATTEMPTS = 8

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    usage = models.OneToOneField(
        "accounts.AITokenUsage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="polar_outbox",
        help_text="Source ledger row; SET_NULL so ledger pruning never blocks",
    )
    # Same key as the ledger row; sent to Polar as the event's external_id,
    # which Polar deduplicates on — the end-to-end exactly-once anchor.
    idempotency_key = models.CharField(max_length=120, unique=True)
    external_customer_id = models.CharField(max_length=64)
    name = models.CharField(max_length=40, default="llm_usage")
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    attempts = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "metering_polar_event_outbox"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at", "created_at"]),
        ]
        verbose_name_plural = "polar event outbox"

    def __str__(self):
        return f"PolarEvent({self.status} {self.name} {self.idempotency_key})"


class PolarCustomer(TimestampMixin):
    """Provisioning marker: this user exists as a Polar customer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="polar_customer"
    )
    polar_customer_id = models.CharField(max_length=64, blank=True, default="")
    # Guards a sandbox -> production switch: rows provisioned against the
    # sandbox org are re-provisioned when the environment changes.
    environment = models.CharField(max_length=12, default="sandbox")
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "metering_polar_customer"

    def __str__(self):
        return f"PolarCustomer({self.user_id}, {self.environment})"


class UsageCounter(models.Model):
    """One monthly count, kept apart from the rows it counts.

    ``meter`` names what is counted:
      - ``prompt_runs``: engine answers held or counted for an account this
        billing period (apps.metering.services.prompt_runs). The value moves
        with a conditional UPDATE, which is what makes the free plan's limit
        hold under concurrent workers.
      - ``deleted_audit_prompts``: prompts of audits deleted this calendar
        month, so deleting an audit never hands its prompts back to the
        monthly prompt allowance (org_entitlements.prompts_used_this_month).
    """

    METER_PROMPT_RUNS = "prompt_runs"
    METER_DELETED_AUDIT_PROMPTS = "deleted_audit_prompts"

    meter = models.CharField(max_length=32)
    # "user:<uuid>" or "org:<uuid>".
    account_key = models.CharField(max_length=64)
    # Period.key: the ISO date the window starts on.
    period_key = models.CharField(max_length=16)
    value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "metering_usage_counter"
        constraints = [
            models.UniqueConstraint(
                fields=["meter", "account_key", "period_key"],
                name="metering_usage_counter_unique",
            ),
        ]

    def __str__(self):
        return f"UsageCounter({self.meter} {self.account_key} {self.period_key}={self.value})"


class PromptRun(models.Model):
    """One prompt answered by one AI engine: the free plan's unit.

    A row is ``reserved`` before the engine is asked and settles once the
    call (retries included) is over: ``counted`` for a successful,
    non-empty answer, ``released`` for anything else. Only reserved and
    counted rows hold a unit of the account's UsageCounter.
    """

    STATUS_RESERVED = "reserved"
    STATUS_COUNTED = "counted"
    STATUS_RELEASED = "released"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Reserved"),
        (STATUS_COUNTED, "Counted"),
        (STATUS_RELEASED, "Released"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_key = models.CharField(max_length=64)
    period_key = models.CharField(max_length=16)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    website = models.ForeignKey(
        "websites.Website", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # Public engine key: chatgpt, claude, gemini, grok, perplexity.
    engine = models.CharField(max_length=16)
    # Where the answer was asked from (prompt_runs.FEATURES).
    feature = models.CharField(max_length=32)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_RESERVED,
    )
    # A retried call (Celery redelivery) reuses its key and is counted once.
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "metering_prompt_run"
        indexes = [
            models.Index(fields=["account_key", "period_key", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"PromptRun({self.status} {self.engine} {self.feature} {self.account_key})"
