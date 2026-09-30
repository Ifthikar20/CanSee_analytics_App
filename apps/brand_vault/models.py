"""Brand Vault models — versioned atomic facts about a brand.

Phase 3 of the GEO platform. Each BrandFact is a (subject, predicate, object)
triple with a validity window and provenance pointers back to the RAG
KnowledgeChunk it was extracted from. Facts are versioned via supersede:
mutations create a new row and link the old one as superseded_by, leaving
an immutable history that the verifier and dashboard can replay.
"""
from __future__ import annotations

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.mixins.timestamp_mixin import TimestampMixin

# Crockford base32: no I, L, O, U — every character survives being read
# aloud or retyped from a screenshot, which is the whole point of a
# human-facing reference.
_REFERENCE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_alert_reference() -> str:
    """Random human-readable alert reference, e.g. ``BSA-7Q4KX2ZC``.

    Random rather than sequential so references are non-enumerable and
    need no counter table; 32^8 combinations make collisions negligible,
    with the column's unique constraint as the backstop.
    """
    return "BSA-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(8))


class FactStatus(models.TextChoices):
    PENDING = "pending", "Pending review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    AUTO = "auto", "Auto-approved"


class BrandFact(TimestampMixin):
    """A versioned atomic fact about a brand."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="facts",
        on_delete=models.CASCADE,
        db_index=True,
    )

    subject = models.CharField(max_length=300, db_index=True)
    predicate = models.CharField(max_length=200, db_index=True)
    object = models.TextField()

    source_chunk = models.ForeignKey(
        "rag.KnowledgeChunk",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="facts",
    )
    source_url = models.URLField(blank=True, max_length=1000)
    extracted_by = models.CharField(max_length=24, default="llm")

    confidence = models.FloatField(default=0.5)
    status = models.CharField(
        max_length=12, choices=FactStatus.choices, default=FactStatus.PENDING,
    )

    version_from = models.DateTimeField(default=timezone.now)
    version_to = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supersedes",
    )

    product_line = models.CharField(max_length=120, blank=True)
    topic = models.CharField(max_length=120, blank=True)
    audience = models.CharField(max_length=120, blank=True)

    embedding = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "brand_vault_brandfact"
        indexes = [
            models.Index(fields=["website", "status", "version_to"], name="bv_fact_w_st_vt_idx"),
            models.Index(fields=["website", "subject", "predicate"], name="bv_fact_w_subj_pred_idx"),
            models.Index(fields=["website", "product_line"], name="bv_fact_w_pl_idx"),
        ]

    def __str__(self):
        return f"BrandFact({self.subject} {self.predicate} {self.object[:40]})"


class FactRevision(TimestampMixin):
    """Immutable audit log of all changes to a BrandFact."""

    ACTION_CREATED = "created"
    ACTION_APPROVED = "approved"
    ACTION_REJECTED = "rejected"
    ACTION_SUPERSEDED = "superseded"
    ACTION_EDITED = "edited"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fact = models.ForeignKey(
        BrandFact, related_name="revisions", on_delete=models.CASCADE,
    )
    action = models.CharField(max_length=24)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        db_table = "brand_vault_factrevision"
        ordering = ["-created_at"]

    def __str__(self):
        return f"FactRevision({self.fact_id}, {self.action})"


class ToneSample(TimestampMixin):
    """A short sample of brand-owned writing for voice modeling.

    Phase 4's voice guard reads these to learn the brand's tone.
    Each sample is a 80-300 word slice from a KnowledgeChunk, deduped
    via ``text_hash`` so re-running the sampler is idempotent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="tone_samples",
        on_delete=models.CASCADE,
    )
    source_chunk = models.ForeignKey(
        "rag.KnowledgeChunk",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tone_samples",
    )
    text = models.TextField()
    text_hash = models.CharField(max_length=64, db_index=True)
    word_count = models.IntegerField(default=0)
    embedding = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "brand_vault_tonesample"
        unique_together = [("website", "text_hash")]
        indexes = [models.Index(fields=["website"], name="bv_tone_w_idx")]

    def __str__(self):
        return f"ToneSample({self.website_id}, {self.word_count}w)"


class SafetyPrompt(TimestampMixin):
    """A user-defined prompt monitored across AI models for brand-safety risks.

    Phase 5 of the GEO platform. Each prompt is re-run on a schedule by the
    safety monitor task; matches are recorded as ``SafetyAlert`` rows.
    """

    STATUS_ACTIVE = "active"
    STATUS_PAUSED = "paused"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_PAUSED, "Paused"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="safety_prompts",
        on_delete=models.CASCADE,
    )
    agent_id = models.CharField(max_length=40, default="llm_truth")
    text = models.CharField(max_length=500)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="created_safety_prompts",
    )

    class Meta:
        db_table = "brand_vault_safetyprompt"
        unique_together = [("website", "agent_id", "text")]
        indexes = [
            models.Index(fields=["website", "status"], name="bv_sp_w_s_idx"),
            models.Index(fields=["website", "agent_id", "status"], name="bv_sp_w_a_s_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"SafetyPrompt({self.website_id}, {self.text[:40]})"


class SafetyAlert(TimestampMixin):
    """A flagged finding raised by a Brand Security agent."""

    SEVERITY_HIGH = "high"
    SEVERITY_MEDIUM = "medium"
    SEVERITY_LOW = "low"
    SEVERITY_CHOICES = [
        (SEVERITY_HIGH, "High"),
        (SEVERITY_MEDIUM, "Medium"),
        (SEVERITY_LOW, "Low"),
    ]

    ISSUE_HALLUCINATION = "hallucination"
    ISSUE_UNVERIFIED = "unverified"
    ISSUE_OUTDATED = "outdated"
    ISSUE_HARMFUL = "harmful"
    ISSUE_NEGATIVE = "negative"
    ISSUE_EMERGING_NARRATIVE = "emerging_narrative"
    ISSUE_NEGATIVE_OUTRANKING = "negative_outranking"
    ISSUE_RANKING_FOR_BAD_QUERY = "ranking_for_bad_query"
    ISSUE_SGE_MISREPRESENTATION = "sge_misrepresentation"
    ISSUE_SENTIMENT_DROP = "sentiment_drop"
    ISSUE_IMPERSONATION = "impersonation"
    ISSUE_DEROGATORY = "derogatory"
    ISSUE_UNFAVORABLE_COMPARISON = "unfavorable_comparison"
    ISSUE_WEAK_ENDORSEMENT = "weak_endorsement"
    ISSUE_DISTRUST = "distrust"
    # Private / non-public information about the brand or its people
    # surfacing inside an AI answer (contact details, credentials,
    # identifiers). Distinct from accuracy: the claim may be TRUE, and
    # that is exactly the problem.
    ISSUE_PRIVATE_DATA = "private_data"
    # Security perception. The answer asserts a certification, compliance
    # status or control the brand's ground truth does not support; blames
    # the brand for an incident the ground truth does not confirm; or
    # advises against the brand on security or privacy grounds.
    ISSUE_HALLUCINATED_COMPLIANCE = "hallucinated_compliance"
    ISSUE_FALSE_INCIDENT = "false_incident"
    ISSUE_SECURITY_ADVISORY = "security_advisory"
    ISSUE_CHOICES = [
        (ISSUE_HALLUCINATION, "Hallucination"),
        (ISSUE_UNVERIFIED, "Unverified claim"),
        (ISSUE_OUTDATED, "Outdated info"),
        (ISSUE_HARMFUL, "Harmful mention"),
        (ISSUE_NEGATIVE, "Negative mention"),
        (ISSUE_EMERGING_NARRATIVE, "Emerging narrative"),
        (ISSUE_NEGATIVE_OUTRANKING, "Negative page outranking"),
        (ISSUE_RANKING_FOR_BAD_QUERY, "Ranking for negative query"),
        (ISSUE_SGE_MISREPRESENTATION, "AI Overview misrepresentation"),
        (ISSUE_SENTIMENT_DROP, "Sentiment drop"),
        (ISSUE_IMPERSONATION, "Impersonation"),
        (ISSUE_DEROGATORY, "Derogatory language"),
        (ISSUE_UNFAVORABLE_COMPARISON, "Unfavorable comparison"),
        (ISSUE_WEAK_ENDORSEMENT, "Weak endorsement"),
        (ISSUE_DISTRUST, "Distrust signals"),
        (ISSUE_PRIVATE_DATA, "Private data exposure"),
        (ISSUE_HALLUCINATED_COMPLIANCE, "Hallucinated compliance claim"),
        (ISSUE_FALSE_INCIDENT, "Unconfirmed security incident"),
        (ISSUE_SECURITY_ADVISORY, "Advised against on security grounds"),
    ]

    SOURCE_LLM = "llm"
    SOURCE_SERP = "serp"
    SOURCE_REDDIT = "reddit"
    SOURCE_X = "x"
    SOURCE_TRENDS = "trends"
    SOURCE_WEB = "web"
    SOURCE_CHOICES = [
        (SOURCE_LLM, "LLM"),
        (SOURCE_SERP, "SERP"),
        (SOURCE_REDDIT, "Reddit"),
        (SOURCE_X, "X"),
        (SOURCE_TRENDS, "Google Trends"),
        (SOURCE_WEB, "Web"),
    ]

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="safety_alerts",
        on_delete=models.CASCADE,
    )
    prompt = models.ForeignKey(
        SafetyPrompt,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="alerts",
    )
    # The exact audit response this finding came from, when the alert was
    # raised by analysing an existing LLM ranking result rather than by
    # re-querying providers. Gives every such alert a traceable origin: the
    # prompt detail page can show "this answer produced these findings",
    # and a reader can read the response the judgement was made against.
    result = models.ForeignKey(
        "llm_ranking.LLMRankingResult",
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name="safety_alerts",
    )

    # Human-readable reference shown in the UI and safe to read aloud,
    # e.g. "BSA-7Q4KX2ZC". Random so alerts cannot be enumerated.
    reference = models.CharField(
        max_length=16, unique=True, editable=False,
        default=generate_alert_reference,
    )
    # Stable detector code from the registry, e.g. "BS-SENT-001". Blank on
    # legacy rows and on rows raised by the (dormant) external-source agents.
    detector_code = models.CharField(max_length=20, blank=True, db_index=True)
    # Exactly which parts of `snippet` triggered the finding:
    # [{"start": int, "end": int, "text": str, "label": str}, ...].
    # Offsets are Unicode-codepoint indices into `snippet` as serialized;
    # `text` echoes the slice so a renderer can verify and re-anchor.
    evidence_spans = models.JSONField(default=list, blank=True)
    # Lifecycle: a finding that keeps recurring on new responses of the same
    # prompt/provider stream is one alert that gets its occurrence bumped,
    # not a fresh row per scan.
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    occurrence_count = models.IntegerField(default=1)
    # sha256(f"{detector_code}|{prompt_key}|{provider}")[:40] — the
    # recurrence grouping key. Blank when no detector code applies.
    dedupe_key = models.CharField(max_length=40, blank=True, db_index=True)

    # Which agent raised this alert. Blank for legacy rows created before agents.
    agent_id = models.CharField(max_length=40, blank=True, db_index=True)
    # Where the underlying signal came from. Blank on legacy rows.
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, blank=True)
    source_url = models.URLField(blank=True, max_length=1000)
    title = models.CharField(max_length=300, blank=True)
    sentiment_score = models.FloatField(null=True, blank=True)

    model = models.CharField(max_length=40, blank=True)
    prompt_text = models.CharField(max_length=500, blank=True)
    snippet = models.TextField()
    issue = models.CharField(max_length=32, choices=ISSUE_CHOICES)
    detail = models.TextField(blank=True)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN,
    )
    detected_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_safety_alerts",
    )

    # RAG grounding trail: list of {chunk_id, source_url, section_label,
    # snippet, score} dicts the judge compared this finding against. Empty
    # when the client has no knowledge base yet or no chunk cleared the
    # relevance threshold.
    evidence_chunks = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "brand_vault_safetyalert"
        indexes = [
            models.Index(fields=["website", "status", "-detected_at"], name="bv_sa_w_s_dt_idx"),
            models.Index(fields=["website", "severity"], name="bv_sa_w_sev_idx"),
            models.Index(fields=["website", "agent_id", "status"], name="bv_sa_w_a_s_idx"),
            models.Index(fields=["website", "detector_code"], name="bv_sa_w_det_idx"),
        ]
        constraints = [
            # One alert per (response, detector). Only enforced where a
            # result is linked — dormant-agent rows have result=NULL.
            models.UniqueConstraint(
                fields=["website", "result", "detector_code"],
                condition=models.Q(result__isnull=False) & ~models.Q(detector_code=""),
                name="bv_sa_res_det_uniq",
            ),
        ]
        ordering = ["-detected_at"]

    def __str__(self):
        return f"SafetyAlert({self.website_id}, {self.agent_id}, {self.severity})"


class BrandSecurityAgent(TimestampMixin):
    """Per-website configuration and status for one Brand Security agent."""

    STATUS_IDLE = "idle"
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_IDLE, "Idle"),
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_ERROR, "Error"),
    ]

    SENSITIVITY_LOW = "low"
    SENSITIVITY_MEDIUM = "medium"
    SENSITIVITY_HIGH = "high"
    SENSITIVITY_CHOICES = [
        (SENSITIVITY_LOW, "Low"),
        (SENSITIVITY_MEDIUM, "Medium"),
        (SENSITIVITY_HIGH, "High"),
    ]

    SCHEDULE_MANUAL = "manual"
    SCHEDULE_HOURLY = "hourly"
    SCHEDULE_DAILY = "daily"
    SCHEDULE_WEEKLY = "weekly"
    SCHEDULE_CHOICES = [
        (SCHEDULE_MANUAL, "Manual"),
        (SCHEDULE_HOURLY, "Hourly"),
        (SCHEDULE_DAILY, "Daily"),
        (SCHEDULE_WEEKLY, "Weekly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="security_agents",
        on_delete=models.CASCADE,
    )
    agent_id = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)
    sensitivity = models.CharField(
        max_length=10, choices=SENSITIVITY_CHOICES, default=SENSITIVITY_MEDIUM,
    )
    schedule = models.CharField(
        max_length=10, choices=SCHEDULE_CHOICES, default=SCHEDULE_DAILY,
    )
    config = models.JSONField(default=dict, blank=True)

    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_IDLE,
    )
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "brand_vault_securityagent"
        unique_together = [("website", "agent_id")]
        indexes = [
            models.Index(fields=["website", "enabled"], name="bv_sag_w_en_idx"),
            models.Index(fields=["enabled", "next_run_at"], name="bv_sag_en_nr_idx"),
        ]

    def __str__(self):
        return f"BrandSecurityAgent({self.website_id}, {self.agent_id})"


class BrandPulse(TimestampMixin):
    """Per-website configuration for the Brand Pulse agent.

    Brand Pulse gathers cross-app brand insights (traffic, AI visibility,
    security alerts, Brand Research findings) into a periodic digest pushed
    to the user's connected chat platforms, and can queue Brand Research
    scans on its own when the data goes stale.
    """

    FREQUENCY_DAILY = "daily"
    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_CHOICES = [
        (FREQUENCY_DAILY, "Daily"),
        (FREQUENCY_WEEKLY, "Weekly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.OneToOneField(
        "websites.Website",
        related_name="pulse",
        on_delete=models.CASCADE,
    )
    enabled = models.BooleanField(default=False)
    # When True the agent may queue Brand Research scans itself when the
    # latest complete scan has gone stale (guarded by the allowance check).
    auto_scan = models.BooleanField(default=True)
    frequency = models.CharField(
        max_length=10, choices=FREQUENCY_CHOICES, default=FREQUENCY_DAILY,
    )
    # Watermarks. last_digest_at doubles as the "what counts as new"
    # boundary for the next digest and as the idempotency gate.
    last_digest_at = models.DateTimeField(null=True, blank=True)
    last_alert_push_at = models.DateTimeField(null=True, blank=True)
    last_scan_queued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "brand_vault_brandpulse"

    def __str__(self):
        state = "enabled" if self.enabled else "disabled"
        return f"BrandPulse({self.website_id}, {self.frequency}, {state})"


class BrandSecurityConfig(TimestampMixin):
    """Per-website global config for Brand Security scanning."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.OneToOneField(
        "websites.Website",
        related_name="security_config",
        on_delete=models.CASCADE,
    )
    brand_terms = models.JSONField(default=list, blank=True)
    negative_keywords = models.JSONField(default=list, blank=True)
    # Per-website switch for the LLM confirmation step on nuanced detectors.
    # The deployment-wide kill switch is settings.BRAND_SECURITY_JUDGE_ENABLED.
    llm_judge_enabled = models.BooleanField(default=True)
    # Watermark for the catch-up scan: responses created after this moment
    # have not yet been swept by the response auditor.
    last_response_scan_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "brand_vault_securityconfig"

    def __str__(self):
        return f"BrandSecurityConfig({self.website_id})"
