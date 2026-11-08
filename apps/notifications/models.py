import uuid

from django.conf import settings
from django.db import models

from core.encryption.field_encryption import EncryptedTextField
from core.mixins.timestamp_mixin import TimestampMixin


class Notification(TimestampMixin):
    """An in-app notification for a user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(max_length=50, db_index=True)
    title = models.CharField(max_length=200)
    message = models.TextField()
    data = models.JSONField(default=dict)
    read = models.BooleanField(default=False, db_index=True)
    action_url = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Notification({self.type}: {self.title})"


class NotificationPreference(models.Model):
    """User notification preferences."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    hot_lead_email = models.BooleanField(default=True)
    hot_lead_slack = models.BooleanField(default=False)
    weekly_report = models.BooleanField(default=True)
    competitor_changes = models.BooleanField(default=True)
    audit_complete = models.BooleanField(default=True)
    slack_webhook_url = EncryptedTextField(blank=True)

    class Meta:
        db_table = "notifications_notificationpreference"

    def __str__(self):
        return f"NotifPrefs({self.user.email})"


class IntegrationConnection(TimestampMixin):
    """A user's connection to an external platform for automated notifications."""

    PLATFORM_CHOICES = [
        ("slack", "Slack"),
        ("discord", "Discord"),
        ("teams", "Microsoft Teams"),
    ]

    FREQUENCY_CHOICES = [
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("realtime", "Real-time"),
    ]

    FORMAT_CHOICES = [
        ("summary", "Summary"),
        ("detailed", "Detailed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="integration_connections"
    )
    organization = models.ForeignKey(
        "accounts.Organization", null=True, blank=True,
        on_delete=models.CASCADE, related_name="integration_connections"
    )
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, db_index=True)
    webhook_url = EncryptedTextField()
    channel_name = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    # Inbound-command linkage: the Slack team_id ("T...") or Discord guild_id
    # (snowflake) this connection is bound to. Inbound events resolve their
    # IntegrationConnection through this id, and only once it is verified.
    # It is set only from a signed Slack/Discord request that carries a
    # link code this account created (services.chat_link), never typed in.
    external_team_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # When the workspace proved the link (see above). Null = not linked.
    external_team_verified_at = models.DateTimeField(null=True, blank=True)
    # Optional default channel id for bot posts (Slack "C...", Discord snowflake).
    external_channel_id = models.CharField(max_length=64, blank=True, default="")

    # Schedule
    schedule_time = models.TimeField(default="09:00")
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default="daily")
    message_format = models.CharField(max_length=20, choices=FORMAT_CHOICES, default="summary")

    # Notification preferences — which message types to send
    notify_daily_report = models.BooleanField(default=True)
    notify_hot_leads = models.BooleanField(default=True)
    notify_trend_digest = models.BooleanField(default=True)
    notify_milestones = models.BooleanField(default=False)

    class Meta:
        db_table = "notifications_integrationconnection"
        unique_together = [("user", "platform")]
        constraints = [
            # One verified owner per Slack workspace / Discord server, so
            # inbound commands always resolve to exactly one account.
            models.UniqueConstraint(
                fields=["platform", "external_team_id"],
                condition=models.Q(external_team_verified_at__isnull=False)
                & ~models.Q(external_team_id=""),
                name="uniq_verified_chat_workspace",
            ),
        ]

    def __str__(self):
        status = "active" if self.is_active else "inactive"
        return f"Integration({self.user.email}, {self.platform}, {status})"


class SmsSubscription(TimestampMixin):
    """A verified mobile number that can receive alerts and reply with questions.

    Not an IntegrationConnection: that model is a webhook URL for a team
    chat platform. A phone number needs three things a webhook does not --
    proof the person owns the number, a durable record of consent, and an
    honoured opt-out. Those obligations are the model, so they live here as
    columns rather than as convention.

    A text is the most intrusive channel in the product, so the default
    posture is narrow: high-severity security alerts and sharp visibility
    drops. The Brand Pulse digest is the one recurring text, gated behind
    its own explicit opt-in (``pulse_digest``, default off) and rendered
    to fit two SMS segments rather than pretending to be a report.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Verified"
        OPTED_OUT = "opted_out", "Opted out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sms_subscriptions",
    )
    # E.164 ("+14155551234"). Encrypted at rest: a phone number is personal
    # data and the column is a standing re-identification key.
    phone_e164 = EncryptedTextField()
    # Non-reversible lookup key. Inbound webhooks arrive with a number, not
    # a user, and an encrypted column cannot be queried -- so matching goes
    # through this digest instead of decrypting every row.
    phone_hash = models.CharField(max_length=64, db_index=True)

    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )

    # --- verification -----------------------------------------------------
    # Hashed, never stored in the clear: possession of the database should
    # not be possession of a live code.
    verification_code_hash = models.CharField(max_length=64, blank=True)
    verification_sent_at = models.DateTimeField(null=True, blank=True)
    verification_attempts = models.PositiveSmallIntegerField(default=0)

    # --- consent record (TCPA) -------------------------------------------
    # Who agreed, when, and from where. Regulators ask for this, and a
    # complaint is answered with it.
    consented_at = models.DateTimeField(null=True, blank=True)
    consent_ip = models.GenericIPAddressField(null=True, blank=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)

    # --- what earns a text ------------------------------------------------
    alert_security = models.BooleanField(default=True)
    alert_visibility_drop = models.BooleanField(default=True)
    # Two-way: replying to a text asks the assistant a question.
    allow_replies = models.BooleanField(default=True)
    # Recurring Brand Pulse digest summaries. Default OFF on purpose: a
    # recurring text carries the heaviest consent burden, so it is an
    # explicit opt-in on top of the number's alert consent — never
    # inferred from it.
    pulse_digest = models.BooleanField(default=False)

    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notifications_smssubscription"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["phone_hash", "status"])]
        # One row per number per user. The same number must not be able to
        # accumulate duplicate subscriptions and get texted twice.
        constraints = [
            models.UniqueConstraint(
                fields=["user", "phone_hash"], name="uniq_sms_user_phone",
            ),
        ]

    def __str__(self):
        return f"SmsSubscription({self.user.email}, {self.status})"

    @property
    def is_active(self) -> bool:
        """Whether this number may be sent to at all."""
        return self.status == self.Status.VERIFIED
