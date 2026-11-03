"""
Retrieval over the website's shared knowledge base.

Top-k cosine similarity, scoped to the website: the knowledge base
belongs to the project, not the person who added it, so every teammate
with access to the website retrieves from the same corpus. The ``user``
argument survives for attribution and spend accounting (the embedder's
allowance wall), never for filtering. With per-website corpora bounded
in the low thousands of chunks, brute-force scoring in Python is
comfortably under 100ms.

Only chunks embedded by a real model are scored. Chunks stored without
a vector (pending) and legacy word-hash vectors are never compared.
When the query cannot be embedded, or no chunk has a vector yet, the
result is empty and carries a reason instead of a guess.

When the corpus grows larger, swap the candidate selection for a
pgvector / ANN index without changing the call sites.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from django.db.models import Count, Q
from django.utils import timezone

from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services.embedder import (
    REASON_NOT_CONFIGURED,
    REASON_PENDING,
    UNEMBEDDED_MODEL_NAMES,
    EmbeddingUnavailable,
    cosine_similarity,
    embed_one,
    embeddings_configured,
)

logger = logging.getLogger("apps")

DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.05

# Recency decay constant: weight = exp(-age_days / 65), a half-life of
# ~45 days. Applied to every source_app except brand_input — the
# user-curated corpus (and every legacy row, which carries the column
# default) is evergreen ground truth and keeps its raw cosine score.
RECENCY_DECAY_DAYS = 65.0

# Human header labels for sources without a real URL (synthetic schemes
# such as llmres:// or gsc://). Derived from the model choices so the
# admin and the prompt blocks always agree.
_SOURCE_APP_LABELS = dict(KnowledgeSource.SOURCE_APP_CHOICES)


@dataclass
class Hit:
    chunk_id: str
    source_id: str
    source_url: str
    source_kind: str
    section_label: str
    text: str
    score: float
    # Provenance (additive; defaults keep pre-existing constructors valid).
    source_app: str = KnowledgeSource.SOURCE_APP_BRAND_INPUT
    metadata: dict = field(default_factory=dict)
    recorded_at: datetime | None = None


class RetrievalHits(list):
    """The hits from :func:`retrieve`, best first.

    ``reason`` is None after a normal search; an empty list then means
    nothing matched (or there is nothing to search). Otherwise retrieval
    could not run and ``reason`` says why: one of the embedder's reason
    codes (``embeddings_not_configured``, ``ai_allowance_exhausted``,
    ``embeddings_failed``) or ``embeddings_pending``.
    """

    def __init__(self, hits=(), reason: str | None = None):
        super().__init__(hits)
        self.reason = reason


def retrieve(
    *,
    user,
    website,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    kinds: list[str] | None = None,
    source_ids: list[str] | None = None,
    source_apps: list[str] | None = None,
    since: datetime | None = None,
) -> RetrievalHits:
    """Return the top ``top_k`` chunks for the query, scoped to the website.

    ``user`` is kept for spend accounting on the query embedding, not
    for filtering — teammates on the same website share retrieval.

    ``source_ids`` narrows retrieval to specific KnowledgeSource rows,
    powering the "test this source" affordance in the Brand Input drawer.
    ``source_apps`` narrows to the producing apps (values from
    ``KnowledgeSource.SOURCE_APP_CHOICES``). ``since`` keeps only chunks
    whose content time — ``recorded_at``, falling back to ``created_at``
    for legacy rows — is at or after the given datetime.

    Ranking: cosine similarity, recency-weighted by
    ``exp(-age_days / RECENCY_DECAY_DAYS)`` for every source_app except
    evergreen ``brand_input``, which keeps the raw cosine score.
    """
    if not query.strip():
        return RetrievalHits()

    qs = KnowledgeChunk.objects.filter(website=website)
    if kinds:
        qs = qs.filter(source__kind__in=kinds)
    if source_ids:
        qs = qs.filter(source_id__in=source_ids)
    if source_apps:
        qs = qs.filter(source__source_app__in=source_apps)
    if since is not None:
        qs = qs.filter(
            Q(recorded_at__gte=since)
            | Q(recorded_at__isnull=True, created_at__gte=since)
        )
    searchable_qs = qs.exclude(embedding_model__in=UNEMBEDDED_MODEL_NAMES)

    counts = qs.aggregate(
        total=Count("id"),
        searchable=Count("id", filter=~Q(embedding_model__in=UNEMBEDDED_MODEL_NAMES)),
    )
    if not counts["total"]:
        return RetrievalHits()
    if not counts["searchable"]:
        reason = REASON_PENDING if embeddings_configured() else REASON_NOT_CONFIGURED
        return RetrievalHits(reason=reason)

    try:
        query_vec, _model, _dim = embed_one(
            query, user=user, website=website,
            metadata={"role": "retrieval_query"},
        )
    except EmbeddingUnavailable as exc:
        return RetrievalHits(reason=exc.reason)

    # ── Optional vector-index path (RAG_VECTOR_BACKEND) ──────────────
    # The index only knows (website, embedding) - the narrowing filters
    # above live in Postgres. So the backend path engages only for the
    # unfiltered case, which is what the assistant's knowledge provider
    # uses; filtered calls keep the exact Python path. Any backend error
    # degrades to the Python path rather than failing retrieval.
    from apps.rag.services.vector_backends import get_backend

    backend = get_backend()
    if backend is not None and not (kinds or source_ids or source_apps or since):
        try:
            backend_hits = _retrieve_via_backend(
                backend, website=website, query_vec=query_vec,
                top_k=top_k, min_score=min_score,
            )
            if backend_hits:
                return RetrievalHits(backend_hits)
            # Zero hits falls through to the Python path rather than
            # returning empty: the flag may be on while the index was
            # never backfilled (or a collection was lost), and trusting
            # the index would then silently empty the knowledge base.
            logger.debug("vector backend returned no hits; using python path")
        except Exception:
            logger.warning(
                "vector backend retrieval failed; falling back to python path",
                exc_info=True,
            )

    candidates = list(searchable_qs.select_related("source").only(
        "id", "embedding", "embedding_dim", "text", "section_label",
        "metadata", "recorded_at", "created_at",
        "source__id", "source__url", "source__kind", "source__source_app",
    ))

    now = timezone.now()
    scored: list[tuple[float, KnowledgeChunk]] = []
    for chunk in candidates:
        if not chunk.embedding or len(chunk.embedding) != len(query_vec):
            # Mixing embedding dimensions across model upgrades: skip
            # mismatched ones rather than corrupting the ranking.
            continue
        score = cosine_similarity(query_vec, chunk.embedding)
        score *= _recency_weight(chunk, now)
        if score >= min_score:
            scored.append((score, chunk))

    scored.sort(key=lambda t: t[0], reverse=True)
    return RetrievalHits(_hit(chunk, score) for score, chunk in scored[:top_k])


def _hit(chunk: KnowledgeChunk, score: float) -> Hit:
    return Hit(
        chunk_id=str(chunk.id),
        source_id=str(chunk.source_id),
        source_url=chunk.source.url,
        source_kind=chunk.source.kind,
        section_label=chunk.section_label,
        text=chunk.text,
        score=float(score),
        source_app=chunk.source.source_app,
        metadata=chunk.metadata or {},
        recorded_at=chunk.recorded_at,
    )


def _retrieve_via_backend(
    backend, *, website, query_vec: list[float], top_k: int, min_score: float,
) -> list[Hit]:
    """Candidate selection through the vector index; everything else is
    unchanged. The index returns (chunk_id, cosine) pairs; the rows are
    then re-fetched from Postgres - which re-asserts website tenant
    scoping no matter what the index contains, and drops ids whose
    chunks have since been deleted (index orphans).

    Over-fetches 3x top_k because recency decay can reorder past the
    cut and min_score can eliminate candidates.
    """
    ranked = backend.query(
        website_id=website.id, vector=query_vec, top_k=top_k * 3,
    )
    if not ranked:
        return []

    by_id = {
        str(c.id): c
        for c in (
            KnowledgeChunk.objects
            .filter(website=website, id__in=[cid for cid, _ in ranked])
            .exclude(embedding_model__in=UNEMBEDDED_MODEL_NAMES)
            .select_related("source")
        )
    }

    now = timezone.now()
    scored: list[tuple[float, KnowledgeChunk]] = []
    for cid, similarity in ranked:
        chunk = by_id.get(str(cid))
        if chunk is None:
            continue
        score = similarity * _recency_weight(chunk, now)
        if score >= min_score:
            scored.append((score, chunk))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [_hit(chunk, score) for score, chunk in scored[:top_k]]


def _recency_weight(chunk: KnowledgeChunk, now: datetime) -> float:
    """Exponential recency decay; 1.0 for evergreen brand_input.

    Age is measured from ``recorded_at`` (content time), falling back to
    ``created_at`` (ingest time) for legacy rows. Future timestamps clamp
    to zero age so nothing scores above its raw cosine.
    """
    if chunk.source.source_app == KnowledgeSource.SOURCE_APP_BRAND_INPUT:
        return 1.0
    stamp = chunk.recorded_at or chunk.created_at
    if stamp is None:  # pragma: no cover — created_at is auto-set
        return 1.0
    age_days = max((now - stamp).total_seconds(), 0.0) / 86400.0
    return math.exp(-age_days / RECENCY_DECAY_DAYS)


def _hit_origin(hit: Hit) -> str:
    """Header origin: URL fragment for real pages, source_app label otherwise.

    Synthetic schemes (``llmres://``, ``gsc://``, ``audit://``...) mean
    nothing to the model reading the prompt, so those hits are labeled by
    what produced them instead.
    """
    scheme = urlsplit(hit.source_url).scheme.lower()
    if scheme in ("http", "https"):
        return hit.source_url[:80]
    return _SOURCE_APP_LABELS.get(
        hit.source_app,
        (hit.source_app or "knowledge").replace("_", " ").capitalize(),
    )


def format_context_block(hits: list[Hit], *, max_chars: int = 3000) -> str:
    """Format hits as a system-prompt-ready block, or "" when there are none."""
    if not hits:
        return ""
    lines = ["=== KNOWLEDGE BASE ==="]
    used = len(lines[0]) + 1
    for hit in hits:
        header = f"\n--- {hit.section_label or hit.source_kind} ({_hit_origin(hit)}) ---"
        body = hit.text.strip()
        block = f"{header}\n{body}"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def retrieve_context_block(
    *,
    user,
    website,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    max_chars: int = 3000,
    kinds: list[str] | None = None,
    source_apps: list[str] | None = None,
    since: datetime | None = None,
) -> str:
    """Return retrieved chunks formatted as a system-prompt-ready block.

    "" when nothing was retrieved, including when retrieval could not run
    (see ``RetrievalHits.reason``).
    """
    hits = retrieve(
        user=user, website=website, query=query,
        top_k=top_k, kinds=kinds,
        source_apps=source_apps, since=since,
    )
    return format_context_block(hits, max_chars=max_chars)
