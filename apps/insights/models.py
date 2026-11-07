"""Insights: measurement runs and what they found (wave 2 contract 1-9).

One ``InsightRun`` per confirmed piece of work (answer analysis, crawler
audit, prompt research, fan-out Google check, shopping check, context
build), with its estimate, what the user confirmed, unit counters and
cost. The other tables keep each kind's results: answer analyses with
their claims and themes, evidence pages, crawler audits, prompt ideas,
Google features read for research, fan-out Google checks, and tracked
products with their shopping results.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.mixins.timestamp_mixin import TimestampMixin


class InsightRun(TimestampMixin):
    """One confirmed run of a kind (contract 1e).

    Nothing runs until the user confirms the estimate; ``confirmed`` keeps
    what they agreed to and ``estimate`` the full estimate at creation.
    The unit counters are only ever updated with F() expressions, so
    workers on several threads count without losing updates.
    """

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_INTERRUPTED = "interrupted"
    ACTIVE = (STATUS_QUEUED, STATUS_RUNNING)
    TERMINAL = (STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="insight_runs",
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
    kind = models.CharField(max_length=24, db_index=True)
    status = models.CharField(max_length=12, default=STATUS_QUEUED, db_index=True)
    params = models.JSONField(default=dict)
    estimate = models.JSONField(default=dict)
    # {ai_calls, ai_usd_max, google_pages} the user confirmed.
    confirmed = models.JSONField(default=dict)
    summary = models.CharField(max_length=200, blank=True)
    units_total = models.PositiveIntegerField(default=0)
    units_running = models.PositiveIntegerField(default=0)
    units_done = models.PositiveIntegerField(default=0)
    units_failed = models.PositiveIntegerField(default=0)
    units_skipped = models.PositiveIntegerField(default=0)
    units_cancelled = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    # Google results pages the provider billed.
    google_pages = models.PositiveIntegerField(default=0)
    google_provider = models.CharField(max_length=16, blank=True)
    # Written at finalize from the usage ledger rows tagged with this run.
    ai_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    # A kind's own outcome (context_build's pages and suggestion counts).
    result = models.JSONField(default=dict, blank=True)
    # Public text only.
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insights_run"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["website", "kind", "-created_at"]),
            models.Index(fields=["website", "status"]),
        ]

    def __str__(self):
        return f"InsightRun({self.kind}, {self.status})"

    @property
    def audit_id(self) -> str:
        """The usage ledger tag of this run's AI requests."""
        return f"insights-run:{self.id}"


# -- answer analysis (contract 3) ----------------------------------------------


class AnswerAnalysis(models.Model):
    """One analysis of one engine answer that names the brand: its claims
    and themes (extract), then a check of the claims (verify)."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    result = models.ForeignKey(
        "llm_ranking.LLMRankingResult",
        related_name="insight_analyses",
        on_delete=models.CASCADE,
    )
    run = models.ForeignKey(InsightRun, on_delete=models.SET_NULL, null=True, blank=True)
    version = models.CharField(max_length=8)
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    error_code = models.CharField(max_length=32, blank=True)
    # Public engine key.
    engine = models.CharField(max_length=16)
    prompt = models.ForeignKey(
        "prompt_library.Prompt", on_delete=models.SET_NULL, null=True, blank=True,
    )
    # The answer's own time (the result's created_at).
    answered_at = models.DateTimeField()
    claims_count = models.PositiveSmallIntegerField(default=0)
    themes_count = models.PositiveSmallIntegerField(default=0)
    # The claims were checked against the evidence.
    verified = models.BooleanField(default=False)
    model = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "insights_answeranalysis"
        constraints = [
            models.UniqueConstraint(fields=["result", "version"], name="insights_analysis_unique"),
        ]
        indexes = [
            models.Index(fields=["website", "version", "-answered_at"]),
        ]

    def __str__(self):
        return f"AnswerAnalysis({self.engine}, {self.status})"


class AnswerClaim(models.Model):
    """A checkable statement an answer makes about the brand, quoted word
    for word from the answer, with the checker's verdict."""

    VERDICT_SUPPORTED = "supported"
    VERDICT_CONTRADICTED = "contradicted"
    VERDICT_NOT_VERIFIABLE = "not_verifiable"
    VERDICT_UNCHECKED = "unchecked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis = models.ForeignKey(AnswerAnalysis, related_name="claims", on_delete=models.CASCADE)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    engine = models.CharField(max_length=16)
    prompt = models.ForeignKey(
        "prompt_library.Prompt", on_delete=models.SET_NULL, null=True, blank=True,
    )
    answered_at = models.DateTimeField()
    text = models.CharField(max_length=300)
    # Groups the same claim across answers (text.claim_key).
    text_key = models.CharField(max_length=16, db_index=True)
    topic = models.CharField(max_length=16)
    quote = models.TextField()
    verdict = models.CharField(max_length=16, default=VERDICT_UNCHECKED)
    # "fact" | "page" | "" (no evidence).
    evidence_kind = models.CharField(max_length=8, blank=True)
    evidence_url = models.URLField(max_length=2000, blank=True)
    evidence_fact = models.ForeignKey(
        "brand_vault.BrandFact", on_delete=models.SET_NULL, null=True, blank=True,
    )
    evidence_quote = models.TextField(blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "insights_answerclaim"
        indexes = [
            models.Index(fields=["website", "-answered_at"]),
        ]

    def __str__(self):
        return f"AnswerClaim({self.verdict}: {self.text[:40]})"


class AnswerTheme(models.Model):
    """How an answer characterizes the brand, quoted word for word."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis = models.ForeignKey(AnswerAnalysis, related_name="themes", on_delete=models.CASCADE)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    engine = models.CharField(max_length=16)
    prompt = models.ForeignKey(
        "prompt_library.Prompt", on_delete=models.SET_NULL, null=True, blank=True,
    )
    answered_at = models.DateTimeField()
    label = models.CharField(max_length=60)
    # Folded label: themes group by (label_key, tone).
    label_key = models.CharField(max_length=60, db_index=True)
    tone = models.CharField(max_length=10)
    quote = models.TextField()

    class Meta:
        db_table = "insights_answertheme"

    def __str__(self):
        return f"AnswerTheme({self.label}, {self.tone})"


class EvidencePage(models.Model):
    """A page of the project's own site read as evidence for claim checks,
    reused for INSIGHTS_EVIDENCE_TTL_HOURS."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    url = models.URLField(max_length=2000)
    final_url = models.URLField(max_length=2000, blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    title = models.CharField(max_length=300, blank=True)
    # The reader view's text, clipped; never shown in full.
    text = models.TextField(blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_evidencepage"
        constraints = [
            models.UniqueConstraint(fields=["website", "url"], name="insights_evidencepage_unique"),
        ]

    def __str__(self):
        return f"EvidencePage({self.url})"


# -- crawler access (contract 5) -------------------------------------------------


class SiteAudit(models.Model):
    """One domain's crawler audit: robots.txt, llms.txt and the pages read.

    ``brand_key`` is stored for display only: competitor keys move when the
    competitor list is edited, so audits are matched to a brand by the apex
    of ``domain``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(InsightRun, related_name="audits", on_delete=models.CASCADE)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    brand_key = models.CharField(max_length=4)
    domain = models.CharField(max_length=253)
    home_url = models.URLField(max_length=2000)
    robots = models.JSONField(default=dict)
    # The robots.txt bytes that were parsed, for re-evaluation.
    robots_text = models.TextField(blank=True)
    bots = models.JSONField(default=list)
    llms_txt = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    audited_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_siteaudit"
        indexes = [
            models.Index(fields=["website", "brand_key", "-audited_at"]),
        ]

    def __str__(self):
        return f"SiteAudit({self.domain})"


class SiteAuditPage(models.Model):
    """One page of an audit and its checks (``true``, ``false`` or ``null``
    for not checked)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit = models.ForeignKey(SiteAudit, related_name="pages", on_delete=models.CASCADE)
    url = models.URLField(max_length=2000)
    final_url = models.URLField(max_length=2000, blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    title = models.CharField(max_length=300, blank=True)
    checks = models.JSONField(default=dict)
    passed = models.PositiveSmallIntegerField(null=True, blank=True)
    total = models.PositiveSmallIntegerField(null=True, blank=True)
    words = models.IntegerField(null=True, blank=True)
    jsonld_types = models.JSONField(default=list)
    canonical = models.URLField(max_length=2000, blank=True)
    # "meta" | "header" | "".
    noindex_source = models.CharField(max_length=8, blank=True)
    blocked_bots = models.JSONField(default=list)
    error_code = models.CharField(max_length=32, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_siteauditpage"

    def __str__(self):
        return f"SiteAuditPage({self.url})"


# -- prompt research (contract 7) ------------------------------------------------


class PromptIdea(models.Model):
    """A question worth tracking, read from stored data: Google's People
    also ask and related searches, the engines' own searches, AI
    suggestions and predicted sub-queries. Never a volume or a score."""

    STATUS_NEW = "new"
    STATUS_DISMISSED = "dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    text = models.CharField(max_length=300)
    # text.idea_key: the agent's query_key rule.
    text_key = models.CharField(max_length=300)
    sources = models.JSONField(default=list)
    # Where it was seen, newest first, at most 10.
    evidence = models.JSONField(default=list)
    seen_prompts = models.PositiveSmallIntegerField(default=0)
    seen_engines = models.PositiveSmallIntegerField(default=0)
    seen_times = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=10, default=STATUS_NEW)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_promptidea"
        constraints = [
            models.UniqueConstraint(fields=["website", "text_key"], name="insights_promptidea_unique"),
        ]

    def __str__(self):
        return f"PromptIdea({self.text[:40]})"


class SerpFeatureCapture(models.Model):
    """Google's questions and related searches on page one for a tracked
    prompt, read by a research run (no rank check is recorded)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    run = models.ForeignKey(InsightRun, on_delete=models.SET_NULL, null=True, blank=True)
    prompt = models.ForeignKey("prompt_library.Prompt", on_delete=models.CASCADE)
    location = models.CharField(max_length=2, blank=True)
    # [{question, url}] and [str].
    questions = models.JSONField(default=list)
    related_searches = models.JSONField(default=list)
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_serpcapture"

    def __str__(self):
        return f"SerpFeatureCapture({self.prompt_id})"


class PromptSuggestion(models.Model):
    """A question Claude Haiku suggested from the brand context (never a
    search measurement)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    run = models.ForeignKey(InsightRun, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.CharField(max_length=300)
    # The id of the context block it came from (audience, offering:2, ...).
    basis = models.CharField(max_length=40)
    why = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_promptsuggestion"

    def __str__(self):
        return f"PromptSuggestion({self.text[:40]})"


# -- query fan-outs (contract 8) ---------------------------------------------------


class FanoutGoogleCheck(models.Model):
    """Google page one for a search an engine ran while answering a tracked
    prompt (no rank check is recorded)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    run = models.ForeignKey(InsightRun, on_delete=models.SET_NULL, null=True, blank=True)
    prompt = models.ForeignKey("prompt_library.Prompt", on_delete=models.CASCADE)
    query = models.CharField(max_length=300)
    query_key = models.CharField(max_length=300)
    location = models.CharField(max_length=2, blank=True)
    # Your first same-site row on page one; None when you are not on it.
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    # Rows Google returned on page one.
    depth = models.PositiveSmallIntegerField(default=0)
    url = models.URLField(max_length=2000, blank=True)
    # [{position, url, domain, title, is_yours, is_competitor}], at most 10.
    top_results = models.JSONField(default=list)
    error_code = models.CharField(max_length=32, blank=True)
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "insights_fanoutcheck"
        indexes = [
            models.Index(fields=["website", "query_key", "-checked_at"]),
        ]

    def __str__(self):
        return f"FanoutGoogleCheck({self.query[:40]}, position={self.position})"


# -- products (contract 9) ---------------------------------------------------------


class TrackedProduct(TimestampMixin):
    """A product the project tracks in Google Shopping and engine answers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=120)
    match_terms = models.JSONField(default=list)
    url = models.URLField(max_length=2000, blank=True)
    queries = models.JSONField(default=list)
    own_sellers = models.JSONField(default=list)
    is_archived = models.BooleanField(default=False)

    class Meta:
        db_table = "insights_trackedproduct"

    def __str__(self):
        return f"TrackedProduct({self.name})"


class ShoppingResult(models.Model):
    """One shopping question checked for one product: a Google Shopping
    page (``engine`` "google_shopping") or one engine's answer."""

    GOOGLE_SHOPPING = "google_shopping"
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(InsightRun, on_delete=models.CASCADE)
    website = models.ForeignKey("websites.Website", on_delete=models.CASCADE)
    product = models.ForeignKey(TrackedProduct, related_name="results", on_delete=models.CASCADE)
    query = models.CharField(max_length=200)
    # "google_shopping" or a public engine key.
    engine = models.CharField(max_length=24)
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    # Contract 9 states: found / not_found / failed / not_checked /
    # not_configured (Google), named / not_named / failed / not_checked.
    state = models.CharField(max_length=16, blank=True)
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    results_count = models.PositiveSmallIntegerField(null=True, blank=True)
    rank = models.PositiveSmallIntegerField(null=True, blank=True)
    matches = models.JSONField(default=list)
    top = models.JSONField(default=list)
    lowest = models.JSONField(default=dict)
    sellers = models.JSONField(default=list)
    sold_by_you = models.BooleanField(null=True, blank=True)
    answer_text = models.TextField(blank=True)
    excerpt = models.CharField(max_length=400, blank=True)
    model_id = models.CharField(max_length=64, blank=True)
    # Public text only.
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insights_shoppingresult"
        indexes = [
            models.Index(fields=["product", "query", "engine", "-checked_at"]),
        ]

    def __str__(self):
        return f"ShoppingResult({self.engine}, {self.state or self.status})"
