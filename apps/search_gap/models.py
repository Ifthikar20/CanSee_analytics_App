import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.search_gap.services.scoring import google_page
from core.mixins.timestamp_mixin import TimestampMixin


class GoogleRankCheck(TimestampMixin):
    """One Google lookup for a tracked prompt.

    Records where the site ranked (``position`` is None when it did not
    appear within ``depth`` results), the first page of results it was up
    against, Google's AI Overview, and what the lookup cost.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="google_rank_checks",
        on_delete=models.CASCADE,
    )
    brand_prompt = models.ForeignKey(
        "prompt_library.BrandPrompt",
        related_name="google_rank_checks",
        on_delete=models.CASCADE,
    )
    query = models.CharField(max_length=500)
    # ISO-2 country the search ran from (BrandPrompt.location); empty is global.
    location = models.CharField(max_length=8, blank=True, default="")
    depth = models.PositiveSmallIntegerField(default=100)
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    url = models.URLField(max_length=2000, blank=True)
    title = models.CharField(max_length=500, blank=True)
    # First page of organic results: [{position, url, domain, title}].
    top_results = models.JSONField(default=list, blank=True)
    # {shown, content_captured, mentions_you, cites_you, references: [{url, domain, title}]}.
    # content_captured False (Google showed an overview whose content was not
    # read): mentions_you and cites_you are None. Older rows lack the key.
    ai_overview = models.JSONField(default=dict, blank=True)
    # Page one's {"questions": [{question, url}], "related_searches": [str]}
    # (People also ask and related searches); {} on older rows and failed checks.
    serp_features = models.JSONField(default=dict, blank=True)
    pages_fetched = models.PositiveSmallIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    error = models.CharField(max_length=200, blank=True)
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "search_gap_googlerankcheck"
        ordering = ["-checked_at"]
        indexes = [
            models.Index(fields=["brand_prompt", "-checked_at"]),
            models.Index(fields=["website", "-checked_at"]),
        ]

    def __str__(self):
        return f"GoogleRankCheck({self.query!r}, position={self.position})"

    @property
    def page(self) -> int | None:
        return google_page(self.position)


class CountryCheckRun(TimestampMixin):
    """One "check these countries" request: Google and the AI engines for a
    set of tracked prompts in up to COUNTRY_CHECK_MAX_COUNTRIES countries.

    Nothing runs until the user confirms the estimate; ``confirmed`` keeps
    what they agreed to and ``estimate`` the full estimate at creation.
    Country answers stay here: they never write LLMRankingResult or
    GoogleRankCheck rows, so the global numbers are unchanged by them.
    """

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
        related_name="country_runs",
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
    status = models.CharField(max_length=12, default=STATUS_QUEUED, db_index=True)
    # Lower-case ISO-2 codes, library Prompt ids, their BrandPrompt ids and
    # public engine keys, each in plan order.
    countries = models.JSONField(default=list)
    prompt_ids = models.JSONField(default=list)
    brand_prompt_ids = models.JSONField(default=list)
    engines = models.JSONField(default=list)
    include_google = models.BooleanField(default=True)
    google_pages = models.PositiveSmallIntegerField(default=1)
    provider = models.CharField(max_length=16, blank=True)
    # {google_pages, engine_answers, ai_usd_max} the user confirmed.
    confirmed = models.JSONField(default=dict)
    estimate = models.JSONField(default=dict)
    # {country: "queued" | "running" | "done"}.
    country_status = models.JSONField(default=dict)
    cancel_requested = models.BooleanField(default=False)
    # Billed Google results pages.
    google_pages_fetched = models.PositiveIntegerField(default=0)
    # Written at finalize from the usage ledger rows tagged with this run.
    ai_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    # Public text only.
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "search_gap_countryrun"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["website", "-created_at"]),
            models.Index(fields=["website", "status"]),
        ]

    def __str__(self):
        return f"CountryCheckRun({self.website_id}, {self.status})"

    @property
    def audit_id(self) -> str:
        """The usage ledger tag of this run's engine answers and extractions."""
        return f"country-run:{self.id}"


class CountryCheckResult(TimestampMixin):
    """One cell of a country check: a (prompt, country, engine) answer, where
    ``engine`` is "google" or a public engine key."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"
    GOOGLE = "google"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        CountryCheckRun,
        related_name="results",
        on_delete=models.CASCADE,
    )
    # Denormalized from the run for latest-per-cell queries.
    website = models.ForeignKey(
        "websites.Website",
        related_name="country_results",
        on_delete=models.CASCADE,
    )
    brand_prompt = models.ForeignKey(
        "prompt_library.BrandPrompt",
        related_name="country_results",
        on_delete=models.CASCADE,
    )
    country = models.CharField(max_length=2)
    engine = models.CharField(max_length=16)
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    # proxy | google_setting (Google), searched | asked (engines).
    method = models.CharField(max_length=16, blank=True)
    # The Google provider, or the engine's provider key.
    provider = models.CharField(max_length=16, blank=True)
    # Where the Google request connected from ("us", or the country).
    proxy_country = models.CharField(max_length=2, blank=True)
    # Google: your best position within ``depth`` (pages fetched x 10).
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    depth = models.PositiveSmallIntegerField(null=True, blank=True)
    url = models.URLField(max_length=2000, blank=True)
    pages_fetched = models.PositiveSmallIntegerField(default=0)
    # Google page 1: [{position, url, domain, title, is_yours, is_competitor}].
    top_results = models.JSONField(default=list, blank=True)
    # google_rank.summarize_ai_overview shape; {} when not read.
    ai_overview = models.JSONField(default=dict, blank=True)
    # Engines: whether the brand was named, where, and in what tone.
    mentioned = models.BooleanField(null=True, blank=True)
    rank = models.PositiveSmallIntegerField(null=True, blank=True)
    sentiment = models.CharField(max_length=16, blank=True)
    # [{name, position}] and source URLs, at most 10 each.
    brands = models.JSONField(default=list, blank=True)
    citations = models.JSONField(default=list, blank=True)
    answer_text = models.TextField(blank=True)
    model_id = models.CharField(max_length=64, blank=True)
    # "heuristic" when the structured extraction fell back.
    extraction_model = models.CharField(max_length=100, blank=True)
    # Public text only.
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "search_gap_countryresult"
        constraints = [
            models.UniqueConstraint(
                fields=["run", "brand_prompt", "country", "engine"],
                name="search_gap_countryresult_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["website", "country", "engine", "-finished_at"]),
            models.Index(fields=["brand_prompt", "country", "engine", "-finished_at"]),
        ]

    def __str__(self):
        return f"CountryCheckResult({self.country}, {self.engine}, {self.status})"
