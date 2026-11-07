"""Celery tasks for the website's shared RAG knowledge base.

``user_id`` on both tasks is attribution (who queued the ingest) and
spend accounting — never a retrieval scope.
"""
import logging

from celery import shared_task

logger = logging.getLogger("apps")


def _result_dict(result) -> dict:
    return {
        "source_id": result.source_id, "url": result.url,
        "status": result.status, "chunk_count": result.chunk_count,
        "skipped_unchanged": result.skipped_unchanged, "error": result.error,
        "embeddings_pending": result.embeddings_pending,
        "embedding_reason": result.embedding_reason,
    }


@shared_task(
    name="apps.rag.tasks.ingest_url",
    bind=True, max_retries=2, default_retry_delay=30, acks_late=True,
)
def ingest_url_task(self, *, user_id: int, website_id: str, url: str,
                    kind: str = "other", title: str = "") -> dict:
    """Background ingest of a single URL into the website's knowledge base."""
    from django.contrib.auth import get_user_model

    from apps.rag.services.ingest_service import ingest_url
    from apps.websites.models import Website

    User = get_user_model()
    user = User.objects.get(pk=user_id)
    website = Website.objects.get(pk=website_id)
    result = ingest_url(
        user=user, website=website, url=url, kind=kind, title=title,
    )
    return _result_dict(result)


@shared_task(
    name="apps.rag.tasks.ingest_site",
    bind=True, max_retries=1, default_retry_delay=60, acks_late=True,
)
def ingest_site_task(self, *, user_id: int, website_id: str, seed_url: str,
                     page_cap: int = 12, depth: int = 1) -> list[dict]:
    """Background crawl + ingest for an entire website."""
    from django.contrib.auth import get_user_model

    from apps.rag.services.ingest_service import ingest_site
    from apps.websites.models import Website

    User = get_user_model()
    user = User.objects.get(pk=user_id)
    website = Website.objects.get(pk=website_id)
    results = ingest_site(
        user=user, website=website, seed_url=seed_url,
        page_cap=page_cap, depth=depth,
    )
    return [_result_dict(r) for r in results]
