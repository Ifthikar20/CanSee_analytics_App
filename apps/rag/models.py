"""
Per-website knowledge base for retrieval-augmented prompt construction.

A KnowledgeSource is one URL the user (or their automated crawler) added
to the knowledge base for a website. KnowledgeChunks are the text
segments extracted from that source; the embedded ones are what the
retriever searches at query time.

Design notes:
- Embeddings are stored as JSON arrays. Postgres JSONB handles this
  efficiently for the per-user corpus sizes we expect (< 10K chunks per
  website). Avoids pulling pgvector into the deployment.
- Cosine similarity is computed in Python on a candidate set scoped to
  (user, website). The scope keeps the candidate set small enough that
  brute-force scoring is fast (sub-100ms for a few thousand chunks).
- The "learns" property comes from accumulating chunks across audits +
  manual additions. Every audit can ingest its own enrichment context
  back into the knowledge base, so subsequent audits draw from a richer
  seed each time.
"""
import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class KnowledgeSource(TimestampMixin):
    """One ingested URL belonging to a (user, website) knowledge base."""

    KIND_HOMEPAGE = "homepage"
    KIND_BLOG = "blog"
    KIND_PRODUCT = "product"
    KIND_DOCS = "docs"
    KIND_REVIEW = "review"
    KIND_OTHER = "other"
    KIND_AUDIT_CONTEXT = "audit_context"
    KIND_CHOICES = [
        (KIND_HOMEPAGE, "Homepage"),
        (KIND_BLOG, "Blog post"),
        (KIND_PRODUCT, "Product page"),
        (KIND_DOCS, "Documentation"),
        (KIND_REVIEW, "Review / mention"),
        (KIND_OTHER, "Other"),
        (KIND_AUDIT_CONTEXT, "Audit context"),
    ]
    # Kinds that hold the brand's own material. Audit context is built by
    # the audit pipeline (partly by an LLM), so it is not the brand's word.
    BRAND_INPUT_KINDS = (
        KIND_HOMEPAGE, KIND_BLOG, KIND_PRODUCT, KIND_DOCS, KIND_REVIEW, KIND_OTHER,
    )

    STATUS_PENDING = "pending"
    STATUS_INGESTING = "ingesting"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_INGESTING, "Ingesting"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    # Which app produced this source. "brand_input" is the original
    # user-curated corpus (URLs + pasted text); it doubles as the value
    # every legacy row adopts via the column default. The rest are
    # automated feeds written by the unified knowledge layer. The human
    # display labels are also what the retriever prints in context-block
    # headers for sources without a real URL — keep them presentable.
    SOURCE_APP_BRAND_INPUT = "brand_input"
    SOURCE_APP_LLM_RESPONSE = "llm_response"
    SOURCE_APP_SECURITY_ALERT = "security_alert"
    # Written only by the removed agents feature; kept so historical rows
    # still display a label.
    SOURCE_APP_AGENT_INSIGHT = "agent_insight"
    SOURCE_APP_SEARCH_QUERIES = "search_queries"
    SOURCE_APP_CITATIONS = "citations"
    SOURCE_APP_PROMPT_NOTES = "prompt_notes"
    SOURCE_APP_CHOICES = [
        (SOURCE_APP_BRAND_INPUT, "Brand input"),
        (SOURCE_APP_LLM_RESPONSE, "AI answers"),
        (SOURCE_APP_SECURITY_ALERT, "Security findings"),
        (SOURCE_APP_AGENT_INSIGHT, "Agent insights"),
        (SOURCE_APP_SEARCH_QUERIES, "Search queries"),
        (SOURCE_APP_CITATIONS, "Citations"),
        (SOURCE_APP_PROMPT_NOTES, "Prompt notes"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="knowledge_sources",
    )
    website = models.ForeignKey(
        "websites.Website",
        on_delete=models.CASCADE,
        related_name="knowledge_sources",
    )
    url = models.URLField(max_length=2000)
    title = models.CharField(max_length=500, blank=True)
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default=KIND_OTHER, db_index=True,
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    # Provenance: which app produced this source, plus an origin
    # reference (origin-row PK / dedupe key) so incremental syncs can
    # update a recurrence instead of duplicating it.
    source_app = models.CharField(
        max_length=24, choices=SOURCE_APP_CHOICES,
        default=SOURCE_APP_BRAND_INPUT, db_index=True,
    )
    source_ref = models.CharField(max_length=64, blank=True)
    # Content fingerprint so we don't re-embed unchanged pages.
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    chunk_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    last_ingested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rag_knowledge_source"
        ordering = ["-created_at"]
        # Uniqueness is per-WEBSITE: the knowledge base belongs to the
        # project, not the person who happened to add a page. The old
        # (user, website, url) key meant two teammates ingesting the same
        # URL created two rows and paid for two embedding runs, each
        # invisible to the other's retrieval. ``user`` remains as
        # attribution (who added it).
        constraints = [
            models.UniqueConstraint(
                fields=["website", "url"],
                name="uq_rag_source_website_url",
            ),
        ]
        # Explicit index names match the hand-written 0001_initial
        # migration so Django's autodetector doesn't propose a no-op
        # rename migration on every makemigrations.
        indexes = [
            models.Index(fields=["user", "website"], name="rag_kn_sou_user_website_idx"),
            models.Index(fields=["website", "kind"], name="rag_kn_sou_website_kind_idx"),
        ]

    def __str__(self):
        return f"KnowledgeSource({self.url[:60]}, {self.status})"


class AgentCrawlConsent(TimestampMixin):
    """Per-website permission for the Cansee agent to crawl the site for
    brand knowledge (``user`` records who granted it).

    The flag is explicit consent: automated crawls (the seed crawl on
    enable today, scheduled re-crawls tomorrow) must check ``enabled``
    before touching the user's site. Disabling never deletes what was
    already ingested — sources stay manageable on the Brand Ingestion
    page.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agent_crawl_consents",
    )
    website = models.ForeignKey(
        "websites.Website",
        on_delete=models.CASCADE,
        related_name="agent_crawl_consents",
    )
    enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    # When we last queued an automated seed crawl — throttles re-seeding
    # when the toggle is flipped off and on again.
    last_seeded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rag_agent_crawl_consent"
        # Consent is per-WEBSITE: one teammate granting the crawl covers
        # the project (``user`` records who granted it). Was per-(user,
        # website), which made each teammate re-consent and re-seed.
        constraints = [
            models.UniqueConstraint(
                fields=["website"],
                name="uq_rag_agent_crawl_website",
            ),
        ]

    def __str__(self):
        return f"AgentCrawlConsent({self.website_id}, enabled={self.enabled})"


class KnowledgeChunk(TimestampMixin):
    """One text segment from a KnowledgeSource, with its embedding when available."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        KnowledgeSource, on_delete=models.CASCADE, related_name="chunks",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="knowledge_chunks",
    )
    website = models.ForeignKey(
        "websites.Website",
        on_delete=models.CASCADE,
        related_name="knowledge_chunks",
    )
    # Order of this chunk within its source (0-indexed).
    chunk_index = models.IntegerField(default=0)
    # Raw text — kept compact via the chunker (≤ ~400 words).
    text = models.TextField()
    # Embedding vector. List of floats, length = embedding model dim
    # (1536 for OpenAI text-embedding-3-small). Empty, with
    # embedding_model "pending", when the chunk was stored while
    # embeddings were unavailable; "hash-256" marks legacy word-hash
    # vectors. Retrieval scores neither; embed_pending_chunks fills them.
    embedding = models.JSONField(default=list)
    embedding_model = models.CharField(max_length=100, blank=True)
    embedding_dim = models.IntegerField(default=0)
    # Optional structural tag (heading text, section type, etc.) so the
    # retriever can show a human-readable snippet header.
    section_label = models.CharField(max_length=200, blank=True)
    token_count = models.IntegerField(default=0)
    # Provenance payload copied from the producing app (provider,
    # severity, detector_code, audit_id, date...). Filter/display only —
    # never embedded, never scored.
    metadata = models.JSONField(default=dict, blank=True)
    # When the underlying content happened (answer generated, alert
    # raised, week observed) as opposed to when it was ingested. Null on
    # legacy rows; the retriever falls back to created_at.
    recorded_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "rag_knowledge_chunk"
        ordering = ["source", "chunk_index"]
        indexes = [
            models.Index(fields=["user", "website"], name="rag_kn_chk_user_website_idx"),
            models.Index(fields=["source", "chunk_index"], name="rag_kn_chk_source_idx_idx"),
        ]

    def __str__(self):
        preview = (self.text or "")[:60].replace("\n", " ")
        return f"KnowledgeChunk({self.source_id}#{self.chunk_index}: {preview})"
