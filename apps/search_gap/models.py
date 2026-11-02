import uuid

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
    # {shown, mentions_you, cites_you, references: [{url, domain, title}]}.
    ai_overview = models.JSONField(default=dict, blank=True)
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
