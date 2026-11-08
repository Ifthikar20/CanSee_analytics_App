"""Content Studio models.

Content Studio is retired: nothing creates or reads these rows any more.
The models and migrations stay so existing rows and the migration graph
remain valid.

* :class:`ContentBrief` — a ranked content gap (visibility, accuracy,
  citation).
* :class:`ContentDraft` — the drafted artefact for a brief.
"""
from __future__ import annotations

import uuid

from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class GapType(models.TextChoices):
    VISIBILITY = "visibility", "Visibility"
    ACCURACY = "accuracy", "Accuracy"
    CITATION = "citation", "Citation"


class ContentFormat(models.TextChoices):
    BLOG = "blog", "Blog"
    FAQ = "faq", "FAQ"
    REDDIT_ANSWER = "reddit_answer", "Reddit answer"
    JSON_LD = "json_ld", "JSON-LD"
    LANDING_PAGE = "landing_page", "Landing page"
    META_DESCRIPTION = "meta_description", "Meta description"


class BriefStatus(models.TextChoices):
    OPEN = "open", "Open"
    DRAFTED = "drafted", "Drafted"
    SKIPPED = "skipped", "Skipped"
    EXPIRED = "expired", "Expired"


class DraftStatus(models.TextChoices):
    GENERATING = "generating", "Generating"
    READY = "ready", "Ready"
    EDITED = "edited", "Edited"
    APPROVED = "approved", "Approved"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"



class ContentBrief(TimestampMixin):
    """A specific content gap to address, ranked by impact."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website", related_name="briefs", on_delete=models.CASCADE,
    )
    gap_type = models.CharField(max_length=16, choices=GapType.choices)
    impact_score = models.FloatField(default=0.0, db_index=True)

    target_format = models.CharField(max_length=20, choices=ContentFormat.choices)
    target_prompt = models.ForeignKey(
        "prompt_library.Prompt", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="briefs",
    )
    target_source_class = models.CharField(max_length=24, blank=True)

    headline = models.CharField(max_length=300)
    description = models.TextField()
    suggested_structure = models.JSONField(default=dict)
    target_keywords = models.JSONField(default=list)
    grounded_facts = models.ManyToManyField(
        "brand_vault.BrandFact", related_name="briefs", blank=True,
    )

    # Idempotency key for duplicate suppression — derived from
    # (gap_type, target_*). Indexed but not unique because we want
    # to allow re-creation after a brief was skipped/expired.
    dedupe_key = models.CharField(max_length=128, blank=True, db_index=True)

    status = models.CharField(
        max_length=12, choices=BriefStatus.choices,
        default=BriefStatus.OPEN, db_index=True,
    )

    class Meta:
        db_table = "content_studio_contentbrief"
        ordering = ["-impact_score", "-created_at"]
        indexes = [
            models.Index(fields=["website", "status", "-impact_score"], name="cs_brief_w_s_imp_idx"),
            models.Index(fields=["website", "gap_type"], name="cs_brief_w_gap_idx"),
        ]

    def __str__(self):
        return f"ContentBrief({self.gap_type}, {self.headline[:40]})"


class ContentDraft(TimestampMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brief = models.ForeignKey(
        ContentBrief, related_name="drafts", on_delete=models.CASCADE,
    )
    website = models.ForeignKey(
        "websites.Website", related_name="drafts", on_delete=models.CASCADE,
    )

    title = models.CharField(max_length=400)
    body_markdown = models.TextField()
    body_html = models.TextField(blank=True)
    json_ld = models.JSONField(null=True, blank=True)

    word_count = models.IntegerField(default=0)

    voice_score = models.FloatField(null=True, blank=True)
    accuracy_score = models.FloatField(null=True, blank=True)
    voice_notes = models.TextField(blank=True)
    accuracy_notes = models.TextField(blank=True)

    status = models.CharField(
        max_length=12, choices=DraftStatus.choices,
        default=DraftStatus.GENERATING, db_index=True,
    )
    generated_by = models.CharField(max_length=24, default="anthropic")
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "content_studio_contentdraft"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["website", "status"], name="cs_draft_w_s_idx"),
            models.Index(fields=["brief"], name="cs_draft_brief_idx"),
        ]

    def __str__(self):
        return f"ContentDraft({self.title[:40]}, rev{self.revision})"


