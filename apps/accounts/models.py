import secrets
import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.mixins.timestamp_mixin import TimestampMixin
from core.utils.constants import FREEMAIL_DOMAINS, OrgRole, Plan, Segment

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin, TimestampMixin):
    """Custom user model with email-based auth and plan tracking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    full_name = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200, blank=True)
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.INDIVIDUAL)
    segment = models.CharField(
        max_length=20, choices=Segment.choices, default=Segment.INDIVIDUAL
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_email_verified = models.BooleanField(default=False)
    onboarding_complete = models.BooleanField(default=False)
    last_daily_brief = models.DateField(null=True, blank=True)

    # Paywall funnel state: set when the user clicks "Continue with the
    # free plan" on /paywall. Null = never dismissed; clearing it (e.g.
    # in the admin) re-arms the paywall for this user.
    paywall_dismissed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the user chose the Free plan from the paywall. "
                  "Null = never dismissed; clear to re-show the paywall.",
    )

    # Calendar-month spend cap across every AI module. 0 = no cap.
    monthly_ai_cost_cap_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Per-user monthly AI spend cap in USD. 0 disables the cap.",
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "accounts_user"
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return self.email

    @property
    def first_name(self):
        return self.full_name.split()[0] if self.full_name else ""

    @property
    def effective_plan(self):
        """Return the plan to use for feature gating (org plan overrides personal plan for enterprise)."""
        if hasattr(self, "_org_cache"):
            return self._org_cache.plan
        membership = (
            self.org_memberships.select_related("organization")  # type: ignore[attr-defined]
            .order_by("created_at")
            .first()
        )
        if membership:
            self._org_cache = membership.organization
            return membership.organization.plan
        return self.plan


class Organization(TimestampMixin):
    """The tenant and billing anchor for a business customer.

    The org's plan is what every member inherits (User.effective_plan /
    plan_limits._resolve_plan_key). Until seat billing lands, the plan is
    set operationally — `manage.py create_org` or the admin.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="owned_orgs")
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.BUSINESS)
    logo_url = models.URLField(blank=True)

    # When True, members cannot use password credentials at all — login,
    # password reset, and password change all answer sso_required. Flip
    # only through OrgService.set_sso_enforcement, which guards against
    # locking the owner out and revokes existing sessions.
    require_sso = models.BooleanField(default=False)

    # The customer's organization id at the SAML bridge — SSOReady's
    # organization external id, or a WorkOS org id (org_...), depending on
    # SSO_BRIDGE_PROVIDER. Set by ops when the customer's IdP connection
    # is created; non-empty is what offers the "saml" sign-in method.
    sso_connection_id = models.CharField(max_length=64, blank=True, default="")

    # Role granted by domain-JIT auto-join. Never owner/admin: first
    # contact with an org through a matching email domain earns membership,
    # not control (defunct-domain-purchase defense).
    default_role = models.CharField(
        max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER
    )

    # Declared per-org session ceiling (minutes). Stored now so enterprise
    # onboarding can record the requirement; token issuance does not
    # enforce it yet.
    session_max_age_minutes = models.PositiveIntegerField(null=True, blank=True)

    # ── Negotiated enterprise package (ops-set; null = plan default) ──
    # Seats: how many people may hold access (members + pending invites).
    # A 20-person company buying 5 seats gets exactly 5 — this number is
    # the billing unit for custom enterprise pricing.
    seat_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Max members incl. pending invites. Empty = plan default.",
    )
    # Dedicated prompts each SEAT may run per calendar month.
    monthly_prompt_allowance = models.IntegerField(
        null=True, blank=True,
        help_text="Prompts per member per month. -1 = unlimited. Empty = plan default.",
    )
    # Per-audit prompt cap override.
    max_prompts_per_audit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Prompts per single run. Empty = plan default.",
    )

    class Meta:
        db_table = "accounts_organization"

    def __str__(self):
        return self.name


class OrganizationMember(TimestampMixin):
    """Membership linking users to an organization."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="members"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="org_memberships")
    role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    invited_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # How this membership came to exist: invite | domain_jit | sso_jit |
    # founder | admin (manage.py create_user --org).
    joined_via = models.CharField(max_length=20, default="invite")

    class Meta:
        db_table = "accounts_organizationmember"
        unique_together = [("organization", "user")]

    def __str__(self):
        return f"{self.user.email} → {self.organization.name} ({self.role})"


class OrgDomain(TimestampMixin):
    """An email domain claimed by an organization.

    Verification tiers matter: a row EXISTS once claimed, but auto-join
    and SSO enforcement only act on rows with ``verified_at`` set. DNS TXT
    (or an operator's ``manual`` review) is the only path to verified —
    a Google ``hd`` claim alone proves an account belongs to the domain's
    Workspace, not that the workspace belongs to this customer.
    """

    METHOD_CHOICES = [
        ("dns_txt", "DNS TXT record"),
        ("google_hd", "Google Workspace hosted domain"),
        ("entra_tid", "Microsoft Entra tenant"),
        ("manual", "Manually verified"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="domains"
    )
    domain = models.CharField(max_length=253, unique=True, db_index=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="dns_txt")
    dns_token = models.CharField(max_length=64, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    auto_join = models.BooleanField(default=True)
    # Entra tenant GUID for the entra_tid method (fast-follow lane).
    entra_tenant_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Re-verification bookkeeping: a verified domain that later drops its
    # TXT record loses verified status after 3 consecutive failed checks.
    last_checked_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "accounts_orgdomain"

    def __str__(self):
        state = "verified" if self.verified_at else "pending"
        return f"{self.domain} ({state})"

    def clean(self):
        domain = (self.domain or "").strip().lower().rstrip(".")
        if not domain or "." not in domain or "@" in domain:
            raise ValidationError({"domain": "Enter a bare domain like acme.com."})
        if domain in FREEMAIL_DOMAINS:
            raise ValidationError(
                {"domain": "Public email providers can't be claimed as a company domain."}
            )
        self.domain = domain

    def save(self, *args, **kwargs):
        # clean() runs on every save, not just ModelForm paths — the
        # freemail blocklist must hold for service-layer writes too.
        self.clean()
        if not self.dns_token:
            self.dns_token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)


class Invitation(TimestampMixin):
    """A pending, emailed invitation to join an organization.

    Only the sha256 of the invite token is stored — the raw token exists
    in the email link alone, so a DB read can't mint an acceptance. A
    membership row is created only on accept; "pending" lives here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField(db_index=True)
    role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_invitation"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_pending_invite",
            )
        ]

    def __str__(self):
        return f"Invitation({self.email} → {self.organization.name})"

    @property
    def is_pending(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)


class SocialIdentity(TimestampMixin):
    """A verified IdP identity linked to a user.

    Keyed on the provider's stable subject (Google ``sub``, Entra ``oid``,
    WorkOS profile ``id``) — never on email, which users can change at the
    IdP. ``tenant`` holds the Google ``hd`` / Entra ``tid`` / WorkOS
    ``organization_id`` observed at link time.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="social_identities"
    )
    provider = models.CharField(
        max_length=20,
        choices=[
            ("google", "Google"),
            ("entra", "Microsoft Entra"),
            ("saml", "SAML (bridge)"),
        ],
    )
    subject = models.CharField(max_length=190)
    tenant = models.CharField(max_length=64, blank=True, default="")
    email_at_link = models.EmailField(blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_socialidentity"
        unique_together = [("provider", "subject", "tenant")]

    def __str__(self):
        return f"{self.provider}:{self.subject} → {self.user.email}"


class UserProfile(TimestampMixin):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    avatar_url = models.URLField(blank=True)
    timezone = models.CharField(max_length=50, default="UTC")
    phone = models.CharField(max_length=20, blank=True)
    bio = models.TextField(blank=True)

    class Meta:
        db_table = "accounts_userprofile"

    def __str__(self):
        return f"Profile({self.user.email})"


class UserPreferences(TimestampMixin):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferences")
    email_notifications = models.BooleanField(default=True)
    weekly_report = models.BooleanField(default=True)
    morning_brief = models.BooleanField(default=True)

    class Meta:
        db_table = "accounts_userpreferences"


class LoginAttempt(models.Model):
    """SOC2: Track all login attempts for audit and brute-force detection."""

    email = models.EmailField(db_index=True)
    ip_address = models.GenericIPAddressField(null=True)
    user_agent = models.CharField(max_length=500, blank=True)
    success = models.BooleanField(default=False)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="login_attempts"
    )
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "accounts_loginattempt"
        indexes = [models.Index(fields=["email", "timestamp"])]

    def __str__(self):
        status = "success" if self.success else "failed"
        return f"LoginAttempt({self.email}, {status})"


class EmailVerificationOTP(models.Model):
    """6-digit OTP for email verification."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_otps")
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "accounts_emailverificationotp"


class PasswordResetToken(models.Model):
    """Secure token for password reset."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "accounts_passwordresettoken"


class AITokenUsage(TimestampMixin):
    """A single AI API call — records model, tokens, cost, and which module made the call."""

    # Every module that actually calls core.ai_tracking.record_usage.
    # Choices are display hygiene (not DB-enforced); keep in sync with the
    # Polar `llm_usage` event metadata's `module` values.
    MODULE_CHOICES = [
        ("llm_ranking", "LLM Ranking"),
        ("rag", "RAG / Embeddings"),
        ("onboarding", "Onboarding scan"),
        ("prompt_library", "Prompt Library"),
        # The agents app was removed 2026-08-24; the choice stays (label
        # unchanged, so no migration) because historical AITokenUsage
        # rows still carry module="agents".
        ("agents", "Hired Agents"),
        ("brand_vault", "Brand Vault"),
        ("brand_security", "Brand Security"),
        ("content_studio", "Content Studio"),
        ("notifications", "Notifications / Chat"),
        ("source_sentiment", "Source Sentiment"),
        ("source_relevance", "Source Relevance"),
        ("brand_research", "Brand Research"),
        ("agent", "Agent workspace"),
    ]

    PROVIDER_CHOICES = [
        ("anthropic", "Anthropic (Claude)"),
        ("openai", "OpenAI (GPT)"),
        ("google", "Google (Gemini)"),
        ("perplexity", "Perplexity"),
        ("meta", "Meta (Llama)"),
        ("mistral", "Mistral AI"),
        ("cohere", "Cohere"),
        ("deepseek", "DeepSeek"),
        ("xai", "xAI (Grok)"),
        ("amazon", "Amazon (Nova / Bedrock)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name="ai_token_usage", null=True, blank=True,
    )
    website = models.ForeignKey(
        "websites.Website", on_delete=models.CASCADE,
        related_name="ai_token_usage", null=True, blank=True,
    )
    module = models.CharField(max_length=20, choices=MODULE_CHOICES, db_index=True)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default="anthropic")
    model_name = models.CharField(max_length=80, help_text="e.g. claude-sonnet-4-20250514")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    estimated_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=6, default=0,
        help_text="Estimated cost in USD based on provider pricing"
    )
    duration_ms = models.IntegerField(default=0, help_text="How long the API call took in ms")
    metadata = models.JSONField(default=dict, blank=True)
    # Dedupe anchor: Celery retries / acks_late redeliveries re-record the
    # same logical call under the same key and collapse to one row. Also
    # sent to Polar as the event external_id (server-side dedupe). NULL on
    # legacy rows; Postgres exempts NULLs from the unique constraint.
    idempotency_key = models.CharField(
        max_length=120, unique=True, null=True, blank=True, default=None
    )

    class Meta:
        db_table = "accounts_aitokenusage"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "module", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.module} | {self.model_name} | {self.total_tokens} tokens"


