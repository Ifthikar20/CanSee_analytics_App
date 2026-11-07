"""Embeddings for Brand Vault facts.

Delegates to ``apps.rag.services.embedder`` so facts share the RAG
knowledge base's vector space. When embeddings are unavailable (no key,
a spent AI allowance, an API error) no vector is produced: callers leave
the fact's embedding NULL and ``refresh_fact_embeddings`` fills it in
once embeddings work.
"""
from __future__ import annotations

import logging

from apps.rag.services.embedder import EmbeddingUnavailable, embed_one

logger = logging.getLogger("apps")


def embed_text(
    text: str, *, user=None, website=None, metadata: dict | None = None,
) -> tuple[list[float] | None, str]:
    """Return ``(vector, model_name)`` for ``text``.

    ``(None, "")`` when the text is empty or no embedding can be produced.
    Pass ``user``/``website`` wherever they are in scope so the spend is
    attributed to the tenant in ai_tracking. ``metadata`` adds flat usage
    tags (e.g. actor="system" for cron work).
    """
    text = (text or "").strip()
    if not text:
        return None, ""
    try:
        vector, model, _dim = embed_one(
            text, user=user, website=website,
            metadata={"role": "fact_embedding", **(metadata or {})},
        )
    except EmbeddingUnavailable as exc:
        logger.info("Brand vault embedding unavailable: %s", exc.reason)
        return None, ""
    return list(vector), model
