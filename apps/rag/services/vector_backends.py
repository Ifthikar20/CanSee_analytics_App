"""Optional vector-index backends for RAG retrieval.

Postgres remains the source of truth for every chunk (text, provenance,
metadata). A backend is a *derived index* over the embeddings only, used
to replace the fetch-everything-and-score-in-Python candidate selection
in retriever.py. If the index is lost it is rebuilt from Postgres; the
reverse is not true, which is why nothing but vectors lives here.

Selected by settings.RAG_VECTOR_BACKEND:

  "python"  (default) No index. retriever.py keeps its current
            behaviour: load all (user, website) chunks, cosine in a loop.
  "chroma"  Embedded ChromaDB (PersistentClient) at RAG_CHROMA_PATH.
            One collection per (website, embedding-dim). In-process,
            no server, HNSW under the hood.

Scope and limits, stated plainly:

- Single-process assumption. Chroma's PersistentClient is not safe for
  concurrent writers from separate processes. Locally (runserver +
  eager Celery) that holds. In production web and celery are separate
  containers WITHOUT a shared filesystem, so this backend as-is cannot
  serve them both - that deployment needs either Chroma's server mode
  (a new service) or the pgvector column instead. This module exists to
  evaluate the retrieval quality and speed locally before that call.
- Collections are keyed by embedding dimension, mirroring the dim-guard
  in retriever.py. Only model vectors are written now; the 256-dim
  collections hold legacy word-hash vectors from older builds, which the
  delete paths still clean up and queries never reach.
- Deleting a KnowledgeSource through the API removes its vectors; bulk
  deletes that bypass the service layer (admin, shell) leave orphans in
  the index. Orphans are harmless at query time - hits are re-fetched
  from Postgres by id, so a vanished chunk simply drops out - and a
  reindex clears them.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger("apps")


class ChromaBackend:
    """Embedded ChromaDB index. See module docstring for scope."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            url = getattr(settings, "RAG_CHROMA_URL", "") or ""
            if url:
                # Server mode: the production topology. web and celery
                # are separate containers, so the index must be a
                # service both can reach over HTTP - this is what a
                # chromadb/chroma container on the compose network (or
                # an ECS service) provides. Embedded mode below is the
                # single-process local/dev path.
                from urllib.parse import urlparse

                parsed = urlparse(url)
                self._client = chromadb.HttpClient(
                    host=parsed.hostname or "localhost",
                    port=parsed.port or 8000,
                    ssl=parsed.scheme == "https",
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
            else:
                self._client = chromadb.PersistentClient(
                    path=str(settings.RAG_CHROMA_PATH),
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
        return self._client

    @staticmethod
    def _collection_name(website_id, dim: int) -> str:
        return f"w_{str(website_id).replace('-', '')}_d{dim}"

    def _collection(self, website_id, dim: int):
        return self._get_client().get_or_create_collection(
            self._collection_name(website_id, dim),
            metadata={"hnsw:space": "cosine"},
        )

    def replace_source(self, *, website_id, source_id, chunk_ids, vectors, dim):
        """Replace every vector belonging to one KnowledgeSource.

        Mirrors ingest_service's delete-then-bulk_create inside its
        transaction: the index converges to exactly the chunks that
        exist for the source.
        """
        coll = self._collection(website_id, dim)
        existing = coll.get(where={"source_id": str(source_id)})
        if existing["ids"]:
            coll.delete(ids=existing["ids"])
        if chunk_ids:
            coll.add(
                ids=[str(c) for c in chunk_ids],
                embeddings=[list(map(float, v)) for v in vectors],
                metadatas=[{"source_id": str(source_id)} for _ in chunk_ids],
            )

    def delete_website(self, *, website_id, dims=(1536, 256)):
        """Drop a website's collections entirely — account deletion path."""
        client = self._get_client()
        for dim in dims:
            try:
                client.delete_collection(self._collection_name(website_id, dim))
            except Exception:  # absent collection is already the goal state
                logger.debug("no d%s collection for website %s", dim, website_id)

    def delete_source(self, *, website_id, source_id, dims=(1536, 256)):
        for dim in dims:
            try:
                coll = self._collection(website_id, dim)
                existing = coll.get(where={"source_id": str(source_id)})
                if existing["ids"]:
                    coll.delete(ids=existing["ids"])
            except Exception:  # collection may not exist for this dim
                logger.debug("vector delete skipped for dim=%s", dim, exc_info=True)

    def query(self, *, website_id, vector, top_k):
        """Return [(chunk_id, cosine_similarity)] best-first.

        Chroma reports cosine *distance*; similarity = 1 - distance,
        which matches what retriever.py's Python loop computes.
        """
        coll = self._collection(website_id, len(vector))
        if coll.count() == 0:
            return []
        res = coll.query(
            query_embeddings=[list(map(float, vector))],
            n_results=min(top_k, coll.count()),
        )
        ids = res["ids"][0]
        dists = res["distances"][0]
        return [(cid, 1.0 - d) for cid, d in zip(ids, dists, strict=True)]


_BACKENDS = {"chroma": ChromaBackend}
_instance = None


def get_backend():
    """Return the configured backend instance, or None for "python".

    Import failures (chromadb not installed where the flag is on) log
    loudly and fall back to None rather than breaking retrieval: the
    Python path over Postgres gives the same results, only slower.
    """
    global _instance
    name = getattr(settings, "RAG_VECTOR_BACKEND", "python")
    if name in (None, "", "python"):
        return None
    if _instance is not None:
        return _instance
    cls = _BACKENDS.get(name)
    if cls is None:
        logger.error("Unknown RAG_VECTOR_BACKEND=%r; using python path", name)
        return None
    try:
        _instance = cls()
        return _instance
    except Exception:
        logger.exception("RAG vector backend %r failed to init; using python path", name)
        return None


def reset_backend_for_tests():
    global _instance
    _instance = None
