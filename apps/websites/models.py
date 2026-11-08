import uuid

from django.conf import settings
from django.db import models

from core.encryption.field_encryption import EncryptedTextField
from core.mixins.soft_delete_mixin import SoftDeleteMixin
from core.mixins.timestamp_mixin import TimestampMixin
from core.utils.constants import UserRole


class Website(SoftDeleteMixin, TimestampMixin):
    """A website tracked by Cansee."""

    PLATFORM_TYPES = [
        ("shopify", "Shopify"),
        ("wordpress", "WordPress"),
        ("woocommerce", "WooCommerce"),
        ("custom", "Custom / Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="websites"
    )
    # The owning organization. NULL = a personal (B2C) project scoped to
    # ``user`` alone; set = every accepted org member can reach it through
    # WebsiteService. ``user`` stays as the creator/fallback owner —
    # SET_NULL here so deleting an org never cascades into project data.
    organization = models.ForeignKey(
        "accounts.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="websites",
        db_index=True,
    )
    url = models.URLField(max_length=500)
    name = models.CharField(max_length=200)
    industry = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True, help_text="Business description for AI visibility analysis")
    topics = models.JSONField(default=list, blank=True, help_text="Selected AI visibility topics/categories")
    competitors = models.JSONField(default=list, blank=True, help_text="Selected competitor brands as [{name, domain}]")
    pixel_key = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    pixel_verified = models.BooleanField(default=False)
    pixel_verified_at = models.DateTimeField(null=True, blank=True)
    platform_type = models.CharField(max_length=20, choices=PLATFORM_TYPES, default="custom", blank=True)
    crawl_status = models.CharField(max_length=20, default="pending")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "websites_website"
        # Uniqueness is scoped to the owning tenant: per-user for personal
        # projects, per-organization for org projects — so two teammates
        # can't fork duplicate projects for the same domain, while two
        # unrelated personal users still can both track example.com.
        constraints = [
            models.UniqueConstraint(
                fields=["user", "url"],
                condition=models.Q(organization__isnull=True),
                name="uniq_personal_site_url",
            ),
            models.UniqueConstraint(
                fields=["organization", "url"],
                condition=models.Q(organization__isnull=False),
                name="uniq_org_site_url",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.url})"


class WebsiteSettings(TimestampMixin):
    website = models.OneToOneField(Website, on_delete=models.CASCADE, related_name="settings")
    track_anonymous = models.BooleanField(default=True)
    notify_hot_leads = models.BooleanField(default=True)
    hot_lead_threshold = models.IntegerField(default=70)
    weekly_report = models.BooleanField(default=True)

    class Meta:
        db_table = "websites_websitesettings"


class WebsiteMembership(TimestampMixin):
    """Team members with role-based access to a website."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="website_memberships"
    )
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.VIEWER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    accepted = models.BooleanField(default=False)

    class Meta:
        db_table = "websites_websitemembership"
        unique_together = [("website", "user")]

    def __str__(self):
        return f"{self.user.email} — {self.role} on {self.website.name}"


class WebhookEndpoint(TimestampMixin):
    """Outbound webhook endpoint — receives platform events via HTTP POST."""

    ALL_EVENTS = [
        "lead.scored",
        "lead.status_changed",
        "lead.created",
        "competitor.change_detected",
        "audit.completed",
        "visitor.identified",
        "campaign.sent",
        "link.clicked",
    ]

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = models.URLField(max_length=500)
    events = models.JSONField(default=list)  # list of event names to subscribe to
    secret = EncryptedTextField(blank=True)  # HMAC-SHA256 signing key
    is_active = models.BooleanField(default=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.IntegerField(default=0)

    class Meta:
        db_table = "websites_webhookendpoint"

    def __str__(self):
        return f"Webhook({self.url[:60]}, {self.website.name})"

    def subscribes_to(self, event: str) -> bool:
        """Return True if this endpoint is subscribed to the given event."""
        return not self.events or event in self.events


class Integration(TimestampMixin):
    """External service integrations — types driven by core.integrations registry."""

    INTEGRATION_TYPES = [
        ("ga", "Google Analytics"),
        ("ga_hosted", "Cansee Google tag"),
        ("cloudflare", "Cloudflare"),
        ("gsc", "Google Search Console"),
    ]

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="integrations")
    type = models.CharField(max_length=20, choices=INTEGRATION_TYPES)
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # e.g. customer_id, channel_id
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "websites_integration"
        unique_together = [("website", "type")]

    def __str__(self):
        return f"{self.get_type_display()} — {self.website.name}"

    @property
    def config(self):
        """Get this integration's config from the registry."""
        from core.integrations import get_registry
        return get_registry().get(self.type)

    def is_token_expired(self) -> bool:
        if not self.token_expires_at:
            return False
        from django.utils import timezone
        return timezone.now() >= self.token_expires_at

    def needs_token_refresh(self, buffer_seconds: int = 900) -> bool:
        """Check if token needs refresh (default: 15 min before expiry)."""
        if not self.token_expires_at:
            return False
        from datetime import timedelta

        from django.utils import timezone
        return timezone.now() >= (self.token_expires_at - timedelta(seconds=buffer_seconds))
