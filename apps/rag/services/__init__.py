"""Public service API for the rag app."""
from apps.rag.services.chunker import Chunk, chunk_text
from apps.rag.services.crawler import (
    crawl_site,
    discover_from_sitemap,
    extract_same_domain_links,
)
from apps.rag.services.embedder import (
    EmbeddingUnavailable,
    cosine_similarity,
    embed_one,
    embed_texts,
)
from apps.rag.services.ingest_service import (
    EmbedPendingResult,
    IngestResult,
    embed_pending_chunks,
    ingest_audit_context,
    ingest_site,
    ingest_url,
)
from apps.rag.services.retriever import (
    Hit,
    RetrievalHits,
    format_context_block,
    retrieve,
    retrieve_context_block,
)

__all__ = [
    "Chunk",
    "chunk_text",
    "crawl_site",
    "discover_from_sitemap",
    "extract_same_domain_links",
    "EmbeddingUnavailable",
    "cosine_similarity",
    "embed_one",
    "embed_texts",
    "EmbedPendingResult",
    "IngestResult",
    "embed_pending_chunks",
    "ingest_audit_context",
    "ingest_site",
    "ingest_url",
    "Hit",
    "RetrievalHits",
    "format_context_block",
    "retrieve",
    "retrieve_context_block",
]
