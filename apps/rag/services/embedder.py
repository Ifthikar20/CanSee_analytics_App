"""
Embedding service for the RAG knowledge base.

OpenAI ``text-embedding-3-small`` (1536 dim) via the OpenAI SDK, which
retries transient API errors itself. When no key is configured, the
account's AI allowance is spent, or the call still fails, no vector is
produced: ``embed_texts`` raises :class:`EmbeddingUnavailable` with a
stable reason code. Callers store text without a vector (marked
``PENDING_MODEL``) or report the reason; nothing stands in for a real
embedding. ``manage.py embed_pending_chunks`` embeds the pending chunks
once embeddings work.

Usage is logged via ``core.ai_tracking.record_usage`` so embedding spend
shows up alongside provider and extraction spend.
"""
from __future__ import annotations

import logging
import math

from django.conf import settings

logger = logging.getLogger("apps")

OPENAI_MODEL = "text-embedding-3-small"
OPENAI_DIM = 1536

# KnowledgeChunk.embedding_model for a chunk stored without a vector.
PENDING_MODEL = "pending"
# Older builds stored 256-dim word-hash vectors under this model name.
# They are not semantic: retrieval ignores them and embed_pending_chunks
# replaces them.
LEGACY_HASH_MODEL = "hash-256"
# embedding_model values that name no real model: chunks needing a vector.
UNEMBEDDED_MODEL_NAMES = (PENDING_MODEL, LEGACY_HASH_MODEL, "")

# Reason codes carried by EmbeddingUnavailable and by the retriever.
REASON_NOT_CONFIGURED = "embeddings_not_configured"
REASON_ALLOWANCE = "ai_allowance_exhausted"
REASON_FAILED = "embeddings_failed"
# Chunks exist but none has a vector yet (retriever only).
REASON_PENDING = "embeddings_pending"

# Soft input limit per chunk — well below the ~8K token model cap so
# even pathological inputs don't blow up the API call.
_MAX_INPUT_CHARS = 8000


class EmbeddingUnavailable(Exception):
    """No embedding could be produced. ``reason`` is one of the codes above."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def embeddings_configured() -> bool:
    return bool(getattr(settings, "OPENAI_API_KEY", "") or "")


def is_model_vector(model_name: str) -> bool:
    """True when ``model_name`` names a real embedding model."""
    return (model_name or "") not in UNEMBEDDED_MODEL_NAMES


def embed_texts(
    texts: list[str],
    *,
    user=None,
    website=None,
    metadata: dict | None = None,
) -> tuple[list[list[float]], str, int]:
    """Embed a batch of strings.

    Returns ``(vectors, model_name, dim)`` with one vector per input.
    Raises :class:`EmbeddingUnavailable` when no key is configured, the
    account's monthly AI allowance is spent, or the API call fails.
    """
    if not texts:
        return [], "", 0

    api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not api_key:
        raise EmbeddingUnavailable(REASON_NOT_CONFIGURED, "OPENAI_API_KEY is not set")

    # Same spend wall as every chat-model call: embeddings are cheap but
    # still metered spend.
    if user is not None:
        from core.llm import allowance_denial

        denial = allowance_denial(user)
        if denial:
            logger.info("Embedding request denied by spend wall: %s", denial)
            raise EmbeddingUnavailable(REASON_ALLOWANCE, denial)

    cleaned = [(_truncate(t) or " ") for t in texts]
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        resp = client.embeddings.create(model=OPENAI_MODEL, input=cleaned)
    except Exception as exc:
        logger.warning("OpenAI embedding failed: %s", exc)
        raise EmbeddingUnavailable(REASON_FAILED, str(exc)[:300]) from exc

    vectors = [d.embedding for d in resp.data]
    if len(vectors) != len(cleaned):
        raise EmbeddingUnavailable(REASON_FAILED, "embedding count mismatch")

    try:
        from core.ai_tracking import record_usage

        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        record_usage(
            module="rag",
            model_name=OPENAI_MODEL,
            input_tokens=tokens_in,
            output_tokens=0,
            user=user,
            website=website,
            metadata={"role": "embedding", **(metadata or {})},
        )
    except Exception:
        logger.debug("embedding usage record failed", exc_info=True)
    return vectors, OPENAI_MODEL, OPENAI_DIM


def embed_one(text: str, **kw) -> tuple[list[float], str, int]:
    """Convenience wrapper for a single string. Raises like ``embed_texts``."""
    vectors, model, dim = embed_texts([text], **kw)
    return vectors[0], model, dim


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. 0 for empty."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) > _MAX_INPUT_CHARS:
        return text[:_MAX_INPUT_CHARS]
    return text
