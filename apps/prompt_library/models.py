"""
Demand-side prompt library models.

Stores broad-industry prompts mined from external sources (Reddit, SerpAPI,
LLM synthesis, etc.) and surfaces them to LLM Ranking audits via the
sampler service. Each audit can persist a ``PromptSampleRun`` recording
exactly which prompts were sampled and the seed used so a sample can be
reproduced or replayed.
"""
import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class PromptSource(models.TextChoices):
    REDDIT = "reddit", "Reddit"
    QUORA = "quora", "Quora"
    SERPAPI = "serpapi", "SerpAPI"
    DATAFORSEO = "dataforseo", "DataForSEO"
    LLM_SYNTH = "llm_synth", "LLM Synthesis"
    GSC_AGGREGATE = "gsc_aggregate", "GSC Aggregate"
    MANUAL = "manual", "Manual"


class IntentBucket(models.TextChoices):
    CATEGORY = "category", "Category"
    COMPARISON = "comparison", "Comparison"
    PROBLEM = "problem", "Problem"
    LOCAL = "local", "Local"


class Industry(TimestampMixin):
    """One row per supported broad industry. The taxonomy is intentionally
    flat (single level) — 20 broad buckets that map onto our prompt mining
    sources without sub-categorisation noise."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "prompt_library_industry"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Prompt(TimestampMixin):
    """A single demand-side prompt. ``text_hash`` is a sha256 of the
    normalised text so we can deduplicate exact matches cheaply at the
    DB layer; near-duplicates are caught upstream by the dedup service."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    industry = models.ForeignKey(
        Industry, related_name="prompts", on_delete=models.CASCADE
    )
    text = models.TextField()
    intent_bucket = models.CharField(max_length=20, choices=IntentBucket.choices)
    language = models.CharField(max_length=8, default="en")
    source = models.CharField(max_length=20, choices=PromptSource.choices)
    source_url = models.URLField(blank=True, max_length=500)
    excerpt = models.TextField(blank=True)
    # The library's ranking score (source type, recency, paraphrase count;
    # see services/scoring_service.py). Not a demand measurement: the API
    # exposes it as ``source_score``. The column keeps its original name.
    demand_score = models.FloatField(default=0.0, db_index=True)
    is_active = models.BooleanField(default=True)
    text_hash = models.CharField(max_length=64, db_index=True)
    # Templated body with {{ variable }} placeholders. When non-empty,
    # `text` is treated as a denormalised preview of the template.
    template_text = models.TextField(
        blank=True,
        help_text="Prompt body with {{ variable }} placeholders. If empty, falls back to text.",
    )
    template_variables = models.JSONField(
        default=list, blank=True,
        help_text="Auto-extracted list of placeholder names",
    )
    STYLE_CHOICES = [
        ("question", "Question"),
        ("story", "Story"),
        ("comparison", "Comparison"),
        ("local", "Local"),
        ("how_to", "How-to"),
        ("listicle", "Listicle"),
    ]
    style = models.CharField(
        max_length=24, choices=STYLE_CHOICES, default="question", db_index=True,
    )
    effectiveness_score = models.FloatField(
        default=0.0, db_index=True,
        help_text="Cached 0-1 score from past audit performance",
    )
    effectiveness_components = models.JSONField(
        default=dict, blank=True,
        help_text="Per-axis breakdown: visibility_hit_rate, citation_yield, claim_yield, coverage_breadth",
    )
    runs_count = models.IntegerField(
        default=0, help_text="Number of audit runs that used this prompt",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "prompt_library_prompt"
        indexes = [
            models.Index(fields=["industry", "intent_bucket", "is_active"]),
        ]
        unique_together = [("industry", "text_hash")]
        ordering = ["-demand_score", "-created_at"]

    def __str__(self):
        return f"Prompt({self.intent_bucket}, {self.text[:60]!r})"


class PromptVariation(TimestampMixin):
    """Paraphrase of a parent prompt. Embeddings stored as a JSON list to
    match the convention used in apps.rag — pgvector is intentionally
    avoided so the deployment topology stays unchanged."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent_prompt = models.ForeignKey(
        Prompt, related_name="variations", on_delete=models.CASCADE
    )
    text = models.TextField()
    text_hash = models.CharField(max_length=64)
    embedding = models.JSONField(null=True, blank=True)
    embedding_model = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "prompt_library_promptvariation"
        unique_together = [("parent_prompt", "text_hash")]


class IndustryTrend(TimestampMixin):
    """Cached Google Trends snapshot for a single industry.

    Pulled via :mod:`apps.prompt_library.services.trends_service` and
    refreshed when older than 24h. We keep the data as JSON arrays
    rather than per-week rows because consumers always read the whole
    series at once and pytrends responses are small.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    industry = models.OneToOneField(
        Industry, related_name="trend", on_delete=models.CASCADE
    )
    # interest_over_time: list of {"week": "YYYY-MM-DD", "value": 0..100}
    interest_over_time = models.JSONField(default=list, blank=True)
    # top_regions: list of {"code": "US", "name": "United States", "value": 0..100}
    top_regions = models.JSONField(default=list, blank=True)
    keyword_used = models.CharField(max_length=120, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "prompt_library_industrytrend"

    def __str__(self):
        return f"IndustryTrend({self.industry.slug})"


class BrandPrompt(TimestampMixin):
    """A library prompt a user has added to their website's brand prompt set.

    Merged with library samples when an audit runs so users always test
    against the prompts they care about. Hard-delete is the correct
    semantic — removing a row means "stop testing this prompt".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="brand_prompts",
        on_delete=models.CASCADE,
    )
    prompt = models.ForeignKey(
        Prompt, related_name="brand_prompts", on_delete=models.CASCADE
    )
    notes = models.TextField(blank=True)
    # User-assigned classification chips (e.g. branded, non-branded,
    # informational, transactional). Free-form so the UI can create new ones.
    tags = models.JSONField(default=list, blank=True)
    # ISO-2 country the prompt is scanned from; routes web-search geo when
    # the prompt is run in an audit. Empty == global/default.
    location = models.CharField(max_length=8, blank=True, default="")
    # Which models scans of this prompt query, as "<provider>:<model_id>"
    # variant ids from apps.llm_ranking.providers.MODEL_VARIANTS (several
    # variants of one provider are allowed). Empty == the default: every
    # configured provider's default model.
    model_variants = models.JSONField(default=list, blank=True)
    # Per-website archive state. Archived prompts stay in the saved list
    # (under the Archived tab) but are excluded from audits. This is a
    # TENANT-OWNED flag: it replaced an earlier design that toggled the
    # shared catalog Prompt.is_active, which let one tenant's archive
    # affect every other tenant that saved the same prompt.
    is_archived = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "prompt_library_brandprompt"
        unique_together = [("website", "prompt")]
        indexes = [models.Index(fields=["website", "-created_at"])]
        ordering = ["-created_at"]

    def __str__(self):
        return f"BrandPrompt(website={self.website_id}, prompt={self.prompt_id})"


class RejectedBrandPrompt(TimestampMixin):
    """A library prompt the user explicitly dismissed from the
    Suggested view. Keeps it out of future suggestions so the page
    doesn't keep recommending what they've already said no to."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="rejected_prompts",
        on_delete=models.CASCADE,
    )
    prompt = models.ForeignKey(
        Prompt, related_name="rejections", on_delete=models.CASCADE,
    )

    class Meta:
        db_table = "prompt_library_rejectedbrandprompt"
        unique_together = [("website", "prompt")]
        indexes = [models.Index(fields=["website", "-created_at"])]
        ordering = ["-created_at"]


class PromptFanout(TimestampMixin):
    """A sub-query an AI engine ran while answering a saved prompt.

    Stored per-(website, prompt) so the Prompt-detail page can show
    'what the model researched' alongside the main prompt response.
    A crawler task fans out the prompt into 4-8 sub-queries before
    hitting each provider, captures them, and writes them here so
    the UI can list them without re-running the prompt.
    """

    SOURCE_CRAWLER = "crawler"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_CRAWLER, "Crawler"),
        (SOURCE_MANUAL, "Manual"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="prompt_fanouts",
        on_delete=models.CASCADE,
    )
    prompt = models.ForeignKey(
        Prompt, related_name="fanouts", on_delete=models.CASCADE,
    )
    text = models.TextField()
    provider = models.CharField(max_length=24, blank=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_CRAWLER)
    # NULL: nothing computes a confidence for a sub-query.
    confidence = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "prompt_library_promptfanout"
        indexes = [
            models.Index(fields=["website", "prompt", "-created_at"]),
        ]
        ordering = ["-created_at"]


class PromptCrawlRun(TimestampMixin):
    """One per attempt to crawl a saved prompt across the providers.

    Tracks status + summary counts so the UI can show 'last crawled
    N min ago, found M sources' without joining four tables.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETE = "complete"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETE, "Complete"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="prompt_crawl_runs",
        on_delete=models.CASCADE,
    )
    prompt = models.ForeignKey(
        Prompt, related_name="crawl_runs", on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    providers = models.JSONField(default=list, blank=True)
    fanout_count = models.IntegerField(default=0)
    source_count = models.IntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "prompt_library_promptcrawlrun"
        indexes = [
            models.Index(fields=["website", "prompt", "-created_at"]),
        ]
        ordering = ["-created_at"]


class PromptSchedule(TimestampMixin):
    """One per saved prompt: run THIS prompt on a cadence.

    The per-prompt analogue of ``llm_ranking.LLMRankingSchedule`` (which
    schedules a whole-website audit). When enabled, the
    ``dispatch_scheduled_prompt_scans`` Celery Beat task finds schedules
    whose ``next_run_at`` has passed, runs a single-prompt crawl
    (``crawl_prompt`` via ``crawl_prompt_for_website``) across the
    configured providers, then advances ``next_run_at``. One schedule per
    ``BrandPrompt``.
    """

    FREQUENCY_DAILY = "daily"
    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_CHOICES = [
        (FREQUENCY_DAILY, "Daily"),
        (FREQUENCY_WEEKLY, "Weekly"),
        (FREQUENCY_MONTHLY, "Monthly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brand_prompt = models.OneToOneField(
        BrandPrompt, on_delete=models.CASCADE, related_name="schedule",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+",
    )
    is_enabled = models.BooleanField(default=True, db_index=True)
    frequency = models.CharField(
        max_length=20, choices=FREQUENCY_CHOICES, default=FREQUENCY_WEEKLY,
    )
    # Scheduling fields
    next_run_at = models.DateTimeField(db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    # Resilience: auto-pause after N consecutive dispatch failures so a
    # broken prompt doesn't keep burning credits. The in-flight guard is
    # done by querying the latest PromptCrawlRun directly (the crawl often
    # runs async, so a last_run FK would be set too late to be reliable).
    consecutive_failures = models.IntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    auto_pause_threshold = models.IntegerField(default=3)

    class Meta:
        db_table = "prompt_library_promptschedule"

    def __str__(self):
        state = "enabled" if self.is_enabled else "disabled"
        return f"PromptSchedule(bp={self.brand_prompt_id}, {self.frequency}, {state})"


class PromptSampleRun(TimestampMixin):
    """Per-audit snapshot: which prompts were sampled and how. The
    ``seed`` field makes the sample reproducible — re-running the
    sampler with the same (industry, seed, strategy, n) yields the
    same prompt set."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit_run = models.OneToOneField(
        "llm_ranking.LLMRankingAudit",
        on_delete=models.CASCADE,
        related_name="prompt_sample_run",
    )
    industry = models.ForeignKey(Industry, on_delete=models.PROTECT)
    seed = models.IntegerField()
    strategy = models.CharField(max_length=32, default="stratified")
    sampled_prompts = models.ManyToManyField(
        Prompt, through="PromptSampleEntry", related_name="sample_runs"
    )

    class Meta:
        db_table = "prompt_library_promptsamplerun"
        ordering = ["-created_at"]


class PromptSampleEntry(models.Model):
    """Through table — preserves the rank/order of each prompt in the
    sample so the UI can show the sequence used by the audit."""

    sample_run = models.ForeignKey(PromptSampleRun, on_delete=models.CASCADE)
    prompt = models.ForeignKey(Prompt, on_delete=models.PROTECT)
    rank = models.IntegerField()

    class Meta:
        db_table = "prompt_library_promptsampleentry"
        unique_together = [("sample_run", "prompt")]
        ordering = ["rank"]


class PromptVariableSet(TimestampMixin):
    """Per-website templating dictionary. One row per website."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.OneToOneField(
        "websites.Website",
        related_name="prompt_variables",
        on_delete=models.CASCADE,
    )
    variables = models.JSONField(
        default=dict, blank=True,
        help_text="key -> value, e.g. {'location': 'Dallas', 'sales_item': 'medical board'}",
    )

    class Meta:
        db_table = "prompt_library_promptvariableset"

    def __str__(self):
        return f"PromptVariableSet({self.website_id})"


class TestEnvironment(TimestampMixin):
    """A named bucket of saved BrandPrompts from the retired Model Test
    page. No endpoint reads or writes it any more; the table is kept so
    existing rows survive.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="test_environments",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=120)
    created_by = models.ForeignKey(
        "accounts.User",
        related_name="created_test_environments",
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    prompts = models.ManyToManyField(
        BrandPrompt,
        related_name="test_environments",
        blank=True,
    )

    class Meta:
        db_table = "prompt_library_testenvironment"
        unique_together = [("website", "name")]
        indexes = [models.Index(fields=["website", "-created_at"])]
        ordering = ["-created_at"]

    def __str__(self):
        return f"TestEnvironment({self.name} · w={self.website_id})"


# ── Benchmark packs ──────────────────────────────────────────────────
# Reference material a website uses as the ground truth its measured
# prompts get scored against. Two source kinds: web URLs (crawled) and
# raw markdown blobs (uploaded). The extract-claims service reads the
# source, asks Claude for atomic factual claims a good LLM answer about
# this business should mention (product names, customers, pricing,
# differentiators), and persists them as BenchmarkClaim rows. When a
# prompt is measured against providers later, each response is scored
# on how many of the pack's claims it covers — that's the URL ×
# provider × claim cube the dashboard renders.

class BenchmarkPack(TimestampMixin):
    SOURCE_URL = "url"
    SOURCE_MARKDOWN = "markdown"
    SOURCE_CHOICES = [
        (SOURCE_URL, "URL"),
        (SOURCE_MARKDOWN, "Markdown"),
    ]

    STATUS_PENDING = "pending"
    STATUS_EXTRACTING = "extracting"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_EXTRACTING, "Extracting"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website", on_delete=models.CASCADE,
        related_name="benchmark_packs",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL,
        related_name="benchmark_packs", null=True, blank=True,
    )
    source_kind = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    # For url sources: the canonical URL. For markdown sources: a
    # user-provided title so packs are identifiable in the list.
    label = models.CharField(max_length=500)
    # url is empty for markdown packs, filled for url packs.
    url = models.URLField(max_length=2000, blank=True)
    # markdown_content is empty for url packs, filled for markdown
    # packs. Truncated at 100k chars to keep the row tractable.
    markdown_content = models.TextField(blank=True)
    # Populated from the crawl step (title/summary from the fetched
    # page). Left empty for markdown packs.
    fetched_title = models.CharField(max_length=500, blank=True)
    fetched_summary = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING,
        db_index=True,
    )
    extraction_error = models.TextField(blank=True)

    class Meta:
        db_table = "prompt_library_benchmarkpack"
        indexes = [
            models.Index(fields=["website", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"BenchmarkPack({self.label[:40]} · {self.source_kind})"


class BenchmarkClaim(models.Model):
    """One atomic factual claim extracted from a BenchmarkPack.

    Kept flat and lightweight so the scoring step can `.values()` and
    ship the whole set to the judge model in one call. `category` is a
    coarse bucket (product / customer / pricing / feature / other) so
    the dashboard can offer a per-category breakdown; the LLM picks it
    at extraction time.
    """

    CATEGORY_CHOICES = [
        ("product", "Product"),
        ("customer", "Customer"),
        ("pricing", "Pricing"),
        ("feature", "Feature"),
        ("proof", "Proof point"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pack = models.ForeignKey(
        BenchmarkPack, on_delete=models.CASCADE,
        related_name="claims",
    )
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default="other")
    # One sentence, self-contained. Should be answerable with yes/no
    # from a candidate LLM response.
    text = models.CharField(max_length=500)
    # Optional short quote from the source that supports the claim.
    # Kept small so the scorer can include it as evidence in prompts.
    evidence = models.CharField(max_length=500, blank=True)
    # Position within the pack, used for stable ordering in the UI.
    ordinal = models.IntegerField(default=0)

    class Meta:
        db_table = "prompt_library_benchmarkclaim"
        ordering = ["ordinal", "id"]
        indexes = [models.Index(fields=["pack", "ordinal"])]

    def __str__(self):
        return f"BenchmarkClaim({self.text[:60]!r})"
