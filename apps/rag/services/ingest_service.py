"""
Ingest pipeline for the RAG knowledge base.

Given a (website, url) pair (plus the acting user for attribution):

    1. Scrape the page (or crawl if requested).
    2. Hash the canonical content; skip if unchanged since last ingest.
    3. Chunk the text with the project chunker.
    4. Embed each chunk in a single batch call.
    5. Persist KnowledgeSource + KnowledgeChunk rows transactionally.

When embeddings are unavailable (no key, a spent AI allowance, an API
error) the chunks are still stored, without a vector and marked pending:
the text stays usable (fact extraction reads it) and retrieval skips it.
``embed_pending_chunks`` fills the vectors in later; re-ingesting an
unchanged source does the same for that source.

The pipeline is the only entrypoint that should be writing chunks —
keep ad-hoc creation out of views and tasks so the (website, url)
uniqueness invariant is respected. The knowledge base belongs to the
project, not the person who added it: a teammate re-ingesting a URL
updates the shared source instead of creating a parallel copy.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.llm_ranking.services.domain_scanner import scan_domain
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services.chunker import chunk_text
from apps.rag.services.crawler import crawl_site
from apps.rag.services.embedder import (
    PENDING_MODEL,
    REASON_ALLOWANCE,
    REASON_FAILED,
    UNEMBEDDED_MODEL_NAMES,
    EmbeddingUnavailable,
    embed_texts,
)

logger = logging.getLogger("apps")

# Chunks per embedding request when filling in pending vectors.
EMBED_BATCH_SIZE = 64


@dataclass
class IngestResult:
    source_id: str
    url: str
    status: str
    chunk_count: int
    skipped_unchanged: bool = False
    error: str | None = None
    # True when some of the source's chunks have no vector yet;
    # embedding_reason is the embedder's reason code.
    embeddings_pending: bool = False
    embedding_reason: str = ""


@dataclass
class EmbedPendingResult:
    embedded: int = 0
    remaining: int = 0
    # Embedder reason code when a batch could not be embedded.
    reason: str = ""


def _dispatch_auto_fact_extraction(website, source: KnowledgeSource) -> None:
    """Auto-extract brand facts after fresh Brand Input chunks land.

    Debounced so a 12-page crawl triggers one extraction pass, not
    twelve: the first ingest in the window claims the cache flag
    (cache.add is atomic) and schedules extraction 120s out so the rest
    of the crawl's pages land first. Only Brand Input triggers it: facts
    must never be minted from LLM-generated audit context or from the AI
    answers and findings mirrored into the knowledge base.
    Extraction itself is idempotent (only chunks without facts are read),
    so overlapping dispatches are harmless.
    """
    try:
        from django.conf import settings

        if not getattr(settings, "BRAND_VAULT_EXTRACTION_ENABLED", True):
            return
        if (
            source.source_app != KnowledgeSource.SOURCE_APP_BRAND_INPUT
            or source.kind not in KnowledgeSource.BRAND_INPUT_KINDS
        ):
            return
        from django.core.cache import cache

        if cache.add(f"bv:auto_extract:{website.id}", 1, timeout=180):
            from apps.brand_vault.tasks import extract_facts_for_website

            extract_facts_for_website.apply_async(
                args=[str(website.id)], countdown=120,
            )
    except Exception as exc:  # pragma: no cover — never block an ingest
        logger.debug("auto fact-extraction dispatch skipped: %s", exc)


def _mirror_source(website_id, source_id) -> None:
    """Copy a source's model vectors into the optional vector index.

    Best-effort: Postgres is the source of truth and the index can be
    rebuilt from it with ``rag_reindex``.
    """
    from apps.rag.services.vector_backends import get_backend

    backend = get_backend()
    if backend is None:
        return
    rows = list(
        KnowledgeChunk.objects
        .filter(source_id=source_id)
        .exclude(embedding_model__in=UNEMBEDDED_MODEL_NAMES)
        .only("id", "embedding")
    )
    rows = [r for r in rows if r.embedding]
    if not rows:
        return
    try:
        backend.replace_source(
            website_id=website_id, source_id=source_id,
            chunk_ids=[r.id for r in rows],
            vectors=[r.embedding for r in rows],
            dim=len(rows[0].embedding),
        )
    except Exception:
        logger.warning("vector index mirror failed for source %s", source_id, exc_info=True)


def ingest_url(
    *,
    user,
    website,
    url: str,
    kind: str = KnowledgeSource.KIND_OTHER,
    title: str = "",
    text: str | None = None,
    source_app: str | None = None,
    source_ref: str | None = None,
    metadata: dict | None = None,
    recorded_at: datetime | None = None,
) -> IngestResult:
    """
    Ingest a single URL. If ``text`` is supplied (e.g. from an audit's
    enrichment step), we use it directly rather than re-fetching the page.

    Provenance passthrough (all optional; defaults preserve the original
    brand-input behavior): ``source_app``/``source_ref`` land on the
    KnowledgeSource, ``metadata``/``recorded_at`` land on every chunk
    written this pass. Source adapters use these so retrieval can filter
    by producing app and weight by content time.
    """
    # Lookup is (website, url) only: the source row is shared across the
    # website's users, so a teammate re-adding the same URL updates the
    # existing row instead of tripping the unique constraint. ``user``
    # is attribution stamped on creation and never changes ownership.
    defaults = {
        "user": user,
        "kind": kind, "title": title or "",
        "status": KnowledgeSource.STATUS_PENDING,
    }
    if source_app:
        defaults["source_app"] = source_app
    if source_ref:
        defaults["source_ref"] = source_ref
    source, created = KnowledgeSource.objects.get_or_create(
        website=website, url=url, defaults=defaults,
    )
    if not created and kind and source.kind != kind:
        # Allow upgrading kind when a more specific tag is supplied.
        source.kind = kind
    if not created and source_app and source.source_app != source_app:
        source.source_app = source_app
    if not created and source_ref and source.source_ref != source_ref:
        source.source_ref = source_ref

    source.status = KnowledgeSource.STATUS_INGESTING
    source.error_message = ""
    source.save(update_fields=[
        "status", "kind", "source_app", "source_ref",
        "error_message", "updated_at",
    ])

    try:
        if text is None:
            scan = scan_domain(url)
            if not scan.get("success"):
                raise RuntimeError(scan.get("error") or "scan failed")
            page_text = scan.get("content_summary") or ""
            page_title = scan.get("business_name") or title or ""
        else:
            page_text = text
            page_title = title

        if not page_text.strip():
            raise RuntimeError("no text extracted")

        digest = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
        if source.content_hash == digest and source.chunk_count > 0:
            source.status = KnowledgeSource.STATUS_READY
            source.last_ingested_at = timezone.now()
            source.save(update_fields=["status", "last_ingested_at", "updated_at"])
            # Unchanged text keeps its chunks (and the facts linked to
            # them); only vectors still missing are filled in. A failed
            # repair leaves the source ready with its chunks pending.
            try:
                pending = embed_pending_chunks(source=source, user=user)
            except Exception:
                logger.warning(
                    "pending-vector repair failed for source %s", source.id, exc_info=True,
                )
                pending = EmbedPendingResult(
                    remaining=pending_chunks(source=source).count(),
                    reason=REASON_FAILED,
                )
            return IngestResult(
                source_id=str(source.id), url=url,
                status=source.status, chunk_count=source.chunk_count,
                skipped_unchanged=True,
                embeddings_pending=pending.remaining > 0,
                embedding_reason=pending.reason,
            )

        chunks = chunk_text(page_text, default_label=page_title or url)
        if not chunks:
            raise RuntimeError("no chunks produced")

        embedding_reason = ""
        try:
            vectors, model, dim = embed_texts(
                [c.text for c in chunks],
                user=user, website=website,
                metadata={"source_id": str(source.id)},
            )
        except EmbeddingUnavailable as exc:
            # Keep the text, store no vector, and mark the chunks pending.
            embedding_reason = exc.reason
            vectors, model, dim = [[] for _ in chunks], PENDING_MODEL, 0

        with transaction.atomic():
            KnowledgeChunk.objects.filter(source=source).delete()
            KnowledgeChunk.objects.bulk_create([
                KnowledgeChunk(
                    source=source, user=user, website=website,
                    chunk_index=i, text=c.text,
                    embedding=vectors[i], embedding_model=model,
                    embedding_dim=dim, section_label=c.section_label,
                    token_count=c.token_count,
                    metadata=dict(metadata) if metadata else {},
                    recorded_at=recorded_at,
                )
                for i, c in enumerate(chunks)
            ])

            # Mirror into the optional vector index once (and only once)
            # the transaction commits, so a rollback never leaves phantom
            # vectors.
            if not embedding_reason:
                transaction.on_commit(
                    lambda wid=website.id, sid=source.id: _mirror_source(wid, sid)
                )
            source.title = (page_title or source.title)[:500]
            source.content_hash = digest
            source.chunk_count = len(chunks)
            source.status = KnowledgeSource.STATUS_READY
            source.last_ingested_at = timezone.now()
            source.save(update_fields=[
                "title", "content_hash", "chunk_count", "status",
                "last_ingested_at", "updated_at",
            ])

        _dispatch_auto_fact_extraction(website, source)

        return IngestResult(
            source_id=str(source.id), url=url,
            status=source.status, chunk_count=len(chunks),
            embeddings_pending=bool(embedding_reason),
            embedding_reason=embedding_reason,
        )

    except Exception as exc:
        logger.warning("RAG ingest failed for %s: %s", url, exc)
        source.status = KnowledgeSource.STATUS_FAILED
        source.error_message = str(exc)[:500]
        source.save(update_fields=["status", "error_message", "updated_at"])
        return IngestResult(
            source_id=str(source.id), url=url,
            status=source.status, chunk_count=0, error=str(exc)[:500],
        )


def ingest_site(
    *,
    user,
    website,
    seed_url: str,
    page_cap: int = 12,
    depth: int = 1,
) -> list[IngestResult]:
    """Crawl a site from ``seed_url`` and ingest each discovered page."""
    pages = crawl_site(seed_url, page_cap=page_cap, depth=depth)
    results: list[IngestResult] = []
    for page in pages:
        if not page.get("success"):
            continue
        results.append(ingest_url(
            user=user, website=website,
            url=page["url"],
            kind=page.get("kind") or KnowledgeSource.KIND_OTHER,
            title=page.get("business_name") or "",
            text=page.get("content_summary") or "",
        ))
    return results


def ingest_audit_context(
    *, user, website, audit_id: str, llm_context: str,
) -> IngestResult | None:
    """
    Persist the enriched system-prompt context produced for an audit so
    the next audit's prompts can draw on it. The "learning" loop.
    """
    if not llm_context.strip():
        return None
    pseudo_url = f"audit://{audit_id}"
    return ingest_url(
        user=user, website=website,
        url=pseudo_url,
        kind=KnowledgeSource.KIND_AUDIT_CONTEXT,
        title=f"Audit context {audit_id}",
        text=llm_context,
    )


def pending_chunks(*, website_id=None, source=None):
    """Chunks without a model vector: pending ones and legacy hash vectors."""
    qs = KnowledgeChunk.objects.filter(embedding_model__in=UNEMBEDDED_MODEL_NAMES)
    if website_id is not None:
        qs = qs.filter(website_id=website_id)
    if source is not None:
        qs = qs.filter(source=source)
    return qs


def embed_pending_chunks(
    *, website_id=None, source=None, user=None, batch_size: int = EMBED_BATCH_SIZE,
) -> EmbedPendingResult:
    """Embed chunks that have no model vector, in place.

    Chunk rows keep their ids, so facts linked to them stay linked.
    Spend goes to ``user`` when given (an interactive
    re-ingest), otherwise to the website owner. A spent allowance skips
    the rest of that website; any other refusal stops the run. The
    result reports how many chunks are still pending and why.
    """
    from apps.websites.models import Website

    qs = pending_chunks(website_id=website_id, source=source)
    pairs = list(qs.order_by().values_list("website_id", "source_id").distinct())
    result = EmbedPendingResult()
    if not pairs:
        return result

    websites = Website.objects.select_related("user").in_bulk(
        {wid for wid, _ in pairs}
    )
    skipped_websites: set = set()
    for wid, sid in pairs:
        if wid in skipped_websites:
            continue
        website = websites.get(wid)
        if website is None:
            continue
        spend_user = user or getattr(website, "user", None)
        chunks = list(
            qs.filter(source_id=sid).order_by("chunk_index").only("id", "text")
        )
        embedded_here = 0
        stop_reason = ""
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            try:
                vectors, model, dim = embed_texts(
                    [c.text for c in batch],
                    user=spend_user, website=website,
                    metadata={
                        "source_id": str(sid),
                        "actor": "user" if user is not None else "system",
                    },
                )
            except EmbeddingUnavailable as exc:
                stop_reason = exc.reason
                break
            now = timezone.now()
            for chunk, vector in zip(batch, vectors, strict=True):
                chunk.embedding = vector
                chunk.embedding_model = model
                chunk.embedding_dim = dim
                chunk.updated_at = now
            KnowledgeChunk.objects.bulk_update(
                batch, ["embedding", "embedding_model", "embedding_dim", "updated_at"],
            )
            embedded_here += len(batch)

        result.embedded += embedded_here
        if embedded_here:
            _mirror_source(wid, sid)
        if stop_reason:
            result.reason = stop_reason
            if stop_reason == REASON_ALLOWANCE:
                skipped_websites.add(wid)
                continue
            break

    result.remaining = pending_chunks(website_id=website_id, source=source).count()
    return result
