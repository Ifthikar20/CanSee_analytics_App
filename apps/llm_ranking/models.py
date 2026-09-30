import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class LLMRankingAudit(TimestampMixin):
    """
    A single LLM ranking audit run for a website.

    The audit queries multiple LLMs with generated prompts to measure how
    prominently the business appears in AI-generated answers.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website", on_delete=models.CASCADE, related_name="llm_ranking_audits"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="llm_ranking_audits"
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    # The prompts that were used for this audit
    prompts = models.JSONField(default=list)
    # Business context used for this audit (snapshot at time of run)
    business_name = models.CharField(max_length=200, blank=True)
    business_description = models.TextField(blank=True)
    industry = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=200, blank=True)
    keywords = models.JSONField(default=list)
    # Extra URLs the user provided for context (blog posts, product pages, etc.)
    # Each entry: {"url": str, "title": str, "success": bool, "summary": str}
    context_urls = models.JSONField(default=list, blank=True)
    # Aggregate scores
    overall_score = models.IntegerField(default=0, db_index=True)  # 0-100
    mention_rate = models.FloatField(default=0.0)   # % of queries where business was mentioned
    # Beta-Binomial posterior mean for mention rate (Beta(2,8) prior). Used
    # for scoring so 1/1 doesn't peg the score at 100% on tiny samples.
    mention_rate_smoothed = models.FloatField(default=0.0)
    avg_mention_rank = models.FloatField(default=0.0)  # avg position when mentioned (lower=better)
    # Plackett-Luce strengths {brand: 0..1} fit across all rankings in this
    # audit. The target brand and every competitor that appeared with a
    # position contribute. Max strength is normalised to 1.0.
    brand_strengths = models.JSONField(default=dict, blank=True)
    # Which providers were queried
    providers_queried = models.JSONField(default=list)
    # Progress tracking for batch job
    queries_completed = models.IntegerField(default=0)
    total_queries = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    # Live pipeline logs — the frontend polls these during a running audit
    # Each entry: {"ts": "ISO8601", "level": "info|warn|success|error", "msg": str}
    audit_logs = models.JSONField(default=list, blank=True)
    # Duration of the audit run in seconds
    duration_seconds = models.FloatField(null=True, blank=True)
    # Token + cost roll-up for this audit (sum of upstream + extraction calls)
    total_tokens = models.IntegerField(default=0)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    # Statistics: number of replicates per (prompt, provider) — N=1 is the current default
    runs_per_query = models.IntegerField(default=1)
    # 95% Wilson CI on the overall mention_rate (percent 0-100)
    mention_rate_ci_lower = models.FloatField(default=0.0)
    mention_rate_ci_upper = models.FloatField(default=0.0)
    # Mean brand-alignment score (0-100) across scored results, set in
    # finalise. Best-effort snapshot: per-result alignment runs async, so
    # dashboards recompute from result rows rather than trusting this.
    alignment_score = models.FloatField(null=True, blank=True)
    # How per-response extraction was performed
    EXTRACTION_HEURISTIC = "heuristic"
    EXTRACTION_LLM = "llm"
    EXTRACTION_CHOICES = [
        (EXTRACTION_HEURISTIC, "Heuristic (regex + lexicon)"),
        (EXTRACTION_LLM, "LLM (Haiku-class model)"),
    ]
    extraction_method = models.CharField(
        max_length=20, choices=EXTRACTION_CHOICES, default=EXTRACTION_HEURISTIC
    )
    # Geographic region for this audit. "global" means no geo flavoring;
    # other codes append a region hint to prompts and route Perplexity
    # web search to that country.
    region = models.CharField(max_length=10, default="global", db_index=True)
    # Where prompts come from for this audit:
    #   vault   — user-curated keyword vault (legacy default)
    #   library — demand-side prompts sampled from the prompt_library app
    #   hybrid  — both, concatenated (library first, then vault)
    PROMPT_SOURCE_VAULT = "vault"
    PROMPT_SOURCE_LIBRARY = "library"
    PROMPT_SOURCE_HYBRID = "hybrid"
    PROMPT_SOURCE_CHOICES = [
        (PROMPT_SOURCE_VAULT, "vault"),
        (PROMPT_SOURCE_LIBRARY, "library"),
        (PROMPT_SOURCE_HYBRID, "hybrid"),
    ]
    prompt_source = models.CharField(
        max_length=16, choices=PROMPT_SOURCE_CHOICES, default=PROMPT_SOURCE_VAULT
    )
    # What the audit measures:
    #   visibility — does the brand surface unprompted (the default; prompts
    #                never name the brand, providers get crawled context)
    #   security   — what models SAY about the brand's security posture.
    #                Prompts name the brand and run COLD: no crawled
    #                context, no RAG block, so the answer is what a real
    #                user would get. Extraction adds security_claims.
    PROBE_KIND_VISIBILITY = "visibility"
    PROBE_KIND_SECURITY = "security"
    PROBE_KIND_CHOICES = [
        (PROBE_KIND_VISIBILITY, "Visibility"),
        (PROBE_KIND_SECURITY, "Security perception"),
    ]
    probe_kind = models.CharField(
        max_length=16, choices=PROBE_KIND_CHOICES, default=PROBE_KIND_VISIBILITY,
        db_index=True,
    )
    # Aggregated citation footprint by country (ISO-2 code -> count).
    # Built in finalise_audit from each result's citation URLs.
    citation_countries = models.JSONField(default=dict, blank=True)
    # Schedule: frequency for recurring audit jobs
    SCHEDULE_NONE = ""
    SCHEDULE_DAILY = "daily"
    SCHEDULE_WEEKLY = "weekly"
    SCHEDULE_MONTHLY = "monthly"
    SCHEDULE_CHOICES = [
        (SCHEDULE_NONE, "One-time"),
        (SCHEDULE_DAILY, "Daily"),
        (SCHEDULE_WEEKLY, "Weekly"),
        (SCHEDULE_MONTHLY, "Monthly"),
    ]
    schedule_frequency = models.CharField(
        max_length=20, choices=SCHEDULE_CHOICES, default=SCHEDULE_NONE, blank=True
    )

    class Meta:
        db_table = "llm_ranking_audit"
        ordering = ["-created_at"]

    def __str__(self):
        return f"LLMRankingAudit({self.website.name}, score={self.overall_score}, {self.status})"

    @property
    def duration_display(self):
        """Human-readable duration string."""
        if self.duration_seconds is None:
            if self.started_at and not self.completed_at:
                from django.utils import timezone
                secs = (timezone.now() - self.started_at).total_seconds()
            else:
                return "—"
        else:
            secs = self.duration_seconds
        if secs < 60:
            return f"{int(secs)}s"
        mins = int(secs // 60)
        remaining_secs = int(secs % 60)
        return f"{mins}m {remaining_secs}s"


class LLMRankingResult(TimestampMixin):
    """
    One result: a single LLM queried with a single prompt.
    Captures whether the business was mentioned and how prominently.
    """

    PROVIDER_CLAUDE = "claude"
    PROVIDER_GPT4 = "gpt4"
    PROVIDER_GEMINI = "gemini"
    PROVIDER_PERPLEXITY = "perplexity"
    PROVIDER_META_LLAMA = "meta_llama"
    PROVIDER_MISTRAL = "mistral"
    PROVIDER_COHERE = "cohere"
    PROVIDER_DEEPSEEK = "deepseek"
    PROVIDER_GROK = "grok"
    PROVIDER_AMAZON_NOVA = "amazon_nova"
    PROVIDER_CHOICES = [
        (PROVIDER_CLAUDE, "Claude (Anthropic)"),
        (PROVIDER_GPT4, "GPT-4 (OpenAI)"),
        (PROVIDER_GEMINI, "Gemini (Google)"),
        (PROVIDER_PERPLEXITY, "Perplexity"),
        (PROVIDER_META_LLAMA, "Meta Llama"),
        (PROVIDER_MISTRAL, "Mistral AI"),
        (PROVIDER_COHERE, "Cohere"),
        (PROVIDER_DEEPSEEK, "DeepSeek"),
        (PROVIDER_GROK, "Grok (xAI)"),
        (PROVIDER_AMAZON_NOVA, "Amazon Nova"),
    ]

    SENTIMENT_POSITIVE = "positive"
    SENTIMENT_NEUTRAL = "neutral"
    SENTIMENT_NEGATIVE = "negative"
    SENTIMENT_NOT_MENTIONED = "not_mentioned"
    SENTIMENT_CHOICES = [
        (SENTIMENT_POSITIVE, "Positive"),
        (SENTIMENT_NEUTRAL, "Neutral"),
        (SENTIMENT_NEGATIVE, "Negative"),
        (SENTIMENT_NOT_MENTIONED, "Not Mentioned"),
    ]

    audit = models.ForeignKey(LLMRankingAudit, on_delete=models.CASCADE, related_name="results")
    # Public, non-sequential identifier exposed in URLs (the chat modal) so
    # rows can't be enumerated/brute-forced via the integer PK.
    public_id = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True,
    )
    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES, db_index=True)
    # The exact model that answered (e.g. "gpt-5.6-terra"). Empty on rows
    # that predate per-prompt model selection; those ran the provider's
    # default model of their day.
    model_id = models.CharField(max_length=64, blank=True, default="")
    # Index of the prompt within the audit's prompts list. Together with
    # (audit, provider) this is the natural idempotency key, so a retried
    # cell task cannot create a duplicate row.
    prompt_index = models.IntegerField(default=0)
    prompt = models.TextField()
    # Full LLM response text
    response_text = models.TextField(blank=True)
    # Whether the business name or a keyword appeared in the response
    is_mentioned = models.BooleanField(default=False, db_index=True)
    # Position of first mention among listed items (1=first, null=not mentioned)
    mention_rank = models.IntegerField(null=True, blank=True)
    # Sentiment of the mention
    sentiment = models.CharField(
        max_length=20, choices=SENTIMENT_CHOICES, default=SENTIMENT_NOT_MENTIONED
    )
    # 0-100 confidence score for the mention analysis
    confidence_score = models.FloatField(default=0.0)
    # Snippet of text around the first mention
    mention_context = models.TextField(blank=True)
    # Whether the LLM query succeeded (False = API error / rate limit)
    query_succeeded = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)
    # Replicate index within a (prompt, provider) pair — 0 for single-run audits
    run_id = models.IntegerField(default=0)
    # Whether the business name appeared as a hyperlink in the response
    is_linked = models.BooleanField(default=False)
    # List of competitor brands mentioned in this response
    # Each entry: {"name": str, "position": int|null, "linked": bool}
    competitors_mentioned = models.JSONField(default=list, blank=True)
    # Which brand the LLM explicitly recommended (if any)
    primary_recommendation = models.CharField(max_length=200, blank=True)
    # URLs cited in the response
    citations = models.JSONField(default=list, blank=True)
    # Which model + prompt version was used to extract structured data
    extraction_model = models.CharField(max_length=100, blank=True)
    extraction_version = models.CharField(max_length=20, blank=True)
    # Brand alignment benchmark (embedding-only). Score is 0-100 to match
    # the sentiment/confidence/overall conventions; NULL means not scored
    # (failed cell, brand absent from the answer, no Brand Input yet, or
    # semantic embeddings unavailable). alignment_detail always carries at
    # least {"version", "status"} once analyzed; alignment_version "" means
    # never analyzed (the backfill's staleness cursor).
    alignment_score = models.FloatField(null=True, blank=True)
    alignment_detail = models.JSONField(default=dict, blank=True)
    alignment_model = models.CharField(max_length=100, blank=True)
    alignment_version = models.CharField(max_length=20, blank=True)
    # Provenance label for the prompt that produced this row. Free-form
    # short string so the frontend can render a badge such as
    # ``Library / Reddit`` or ``Vault``. Empty for legacy rows.
    prompt_source_label = models.CharField(max_length=64, blank=True, default="")
    # Optional FK to the prompt_library Prompt that produced this row. Lets
    # the effectiveness scorer roll signals back per-template without
    # text-matching across filled variants.
    source_prompt = models.ForeignKey(
        "prompt_library.Prompt",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ranking_results",
    )
    # Per-result citation country breakdown (ISO-2 -> count). Computed
    # from this row's citations during aggregation; rolled up into the
    # audit-level ``citation_countries`` field for the dashboard.
    citation_countries = models.JSONField(default=dict, blank=True)
    # Security-perception extraction (probe_kind == "security" audits and
    # prompts tagged "security"). Shape, written by
    # SecurityClaimExtractionService and enriched by the claim-support
    # scorer:
    #   {"version": "sec-v1", "model": "...",
    #    "claims": [{"claim", "kind", "polarity", "cited_url",
    #                "support", "similarity"}],
    #    "fud_language": bool, "recommends_against": bool,
    #    "support_version": "..."}
    # Empty dict on rows that were never security-extracted.
    security_claims = models.JSONField(default=dict, blank=True)
    # The intent type of the prompt (recommendation, comparison, persona, etc.)
    # NOTE: computed at runtime from prompt text — no database column needed.
    PROMPT_TYPE_CHOICES = [
        ("recommendation", "Recommendation"),
        ("comparison", "Comparison"),
        ("use_case", "Use Case"),
        ("alternatives", "Alternatives"),
        ("category", "Category"),
        ("persona", "Persona"),
        ("review", "Review"),
        ("local", "Local"),
        ("custom", "Custom"),
    ]
    _PROMPT_TYPE_MAP = dict(PROMPT_TYPE_CHOICES)

    @property
    def prompt_type(self):
        """Infer prompt intent from the prompt text."""
        p = (self.prompt or "").lower()
        if any(k in p for k in ("compare", "comparison", "side-by-side", "vs")):
            return "comparison"
        if any(k in p for k in ("recommend", "best", "top", "should i", "which")):
            return "recommendation"
        if any(k in p for k in ("use case", "scenario", "startup", "enterprise")):
            return "use_case"
        if any(k in p for k in ("alternative", "instead of", "replace")):
            return "alternatives"
        if any(k in p for k in ("review", "opinion", "rating")):
            return "review"
        if any(k in p for k in ("near me", "local", "in my area")):
            return "local"
        return "custom"

    def get_prompt_type_display(self):
        return self._PROMPT_TYPE_MAP.get(self.prompt_type, "Custom")

    class Meta:
        db_table = "llm_ranking_result"
        indexes = [
            models.Index(fields=["audit", "provider"]),
            models.Index(fields=["audit", "is_mentioned"]),
        ]
        # Idempotency: at most one row per (audit, prompt_index, provider).
        # Lets the chord fan-out's per-cell task safely retry without making
        # duplicate rows. run_id distinguishes replicate runs of the same cell
        # so the unique key is the full triple plus run_id.
        constraints = [
            # model_id joined the key when per-prompt model selection
            # landed: one crawl may query several variants of the same
            # provider. Audit-pipeline rows all carry model_id="" so their
            # retry idempotency is unchanged.
            models.UniqueConstraint(
                fields=["audit", "prompt_index", "provider", "model_id", "run_id"],
                name="uq_llm_result_audit_prompt_provider_run",
            ),
        ]

    def __str__(self):
        mentioned = "mentioned" if self.is_mentioned else "not mentioned"
        return f"LLMResult({self.provider}, {mentioned}, rank={self.mention_rank})"


class LLMRankingSchedule(TimestampMixin):
    """
    Per-website periodic LLM ranking audit schedule.

    When enabled, a Celery Beat task checks for schedules whose next_run_at
    has passed and creates + queues a new LLMRankingAudit automatically.
    """

    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_BIWEEKLY = "biweekly"
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_CHOICES = [
        (FREQUENCY_WEEKLY, "Weekly"),
        (FREQUENCY_BIWEEKLY, "Every 2 Weeks"),
        (FREQUENCY_MONTHLY, "Monthly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.OneToOneField(
        "websites.Website", on_delete=models.CASCADE, related_name="llm_ranking_schedule"
    )
    # SET_NULL, not CASCADE: the schedule is the WEBSITE's recurring audit.
    # A teammate deleting their account must not silently kill the org's
    # scheduled runs; execution falls back to the website owner when null.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    is_enabled = models.BooleanField(default=True, db_index=True)
    frequency = models.CharField(
        max_length=20, choices=FREQUENCY_CHOICES, default=FREQUENCY_WEEKLY
    )
    # Business context for auto-generated audits
    business_name = models.CharField(max_length=200)
    business_description = models.TextField(blank=True)
    industry = models.CharField(max_length=100)
    location = models.CharField(max_length=200, blank=True)
    keywords = models.JSONField(default=list)
    providers = models.JSONField(
        default=list,
        help_text="LLM providers to query. Empty = all.",
    )
    # Scheduling fields
    next_run_at = models.DateTimeField(db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    # Resilience fields. ``last_audit`` lets the dispatcher skip while a
    # previous run is still in flight. ``consecutive_failures`` auto-pauses
    # the schedule after N failures so we don't keep burning credits on a
    # broken integration. ``last_failure_at`` surfaces in the UI for
    # operator triage.
    last_audit = models.ForeignKey(
        "LLMRankingAudit", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    consecutive_failures = models.IntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    # Auto-pause threshold. 3 consecutive failures (default for weekly =
    # 3 weeks of broken runs) flips is_enabled=False and surfaces a
    # banner. Operator must explicitly re-enable to resume.
    auto_pause_threshold = models.IntegerField(default=3)

    class Meta:
        db_table = "llm_ranking_schedule"

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"LLMSchedule({self.website.name}, {self.frequency}, {status})"
