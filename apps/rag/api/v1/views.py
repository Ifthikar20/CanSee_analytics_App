"""
API for the website's shared RAG knowledge base.

All endpoints are website-scoped via TenantScopedAPIView, and website
access is the whole access check: the knowledge base belongs to the
project, not the person who added it, so every user the tenancy gate
admits to a website sees the same corpus. request.user is still stamped
on writes (attribution) and still keys the rate-limit buckets.
"""
import hashlib
import logging
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.rag.api.v1.serializers import (
    HitSerializer,
    IngestURLSerializer,
    KnowledgeChunkSerializer,
    KnowledgeSourceSerializer,
    RetrieveSerializer,
    UploadTextSerializer,
)
from apps.rag.models import AgentCrawlConsent, KnowledgeChunk, KnowledgeSource
from apps.rag.services.ingest_service import ingest_url, pending_chunks
from apps.rag.services.retriever import retrieve
from core.interceptors.pagination import StandardPagination
from core.interceptors.response_envelope import PageResponse
from core.resilience import TokenBucket
from core.views.base import TenantScopedAPIView

logger = logging.getLogger("apps")


def _ingest_bucket(user_id) -> TokenBucket:
    """
    Per-user token bucket for RAG ingest. Caps abuse where one tenant
    queues huge crawl jobs back-to-back to either rack up embedding cost
    or use the server as a scraper proxy.

    Steady state: 30/min. Burst: 10. Refill is continuous so a real user
    triggering an "Add URL" ten times in a row succeeds; a script firing
    at 100 RPS gets throttled within seconds.
    """
    return TokenBucket(
        name=f"rag-ingest:{user_id}",
        capacity=10,
        refill_per_second=0.5,  # 30/min steady state
    )


def _retrieve_bucket(user_id) -> TokenBucket:
    """Per-user bucket for the cosine-similarity endpoint."""
    return TokenBucket(
        name=f"rag-retrieve:{user_id}",
        capacity=20,
        refill_per_second=2.0,  # 120/min steady state
    )


def _source_stats(qs) -> dict:
    """Aggregate counts by status for the filter chips.

    Runs a single COUNT-with-FILTER query so the badges next to each
    status filter reflect the unfiltered population, not the current
    page — keeps the filter bar useful when a query has narrowed the list.
    """
    return qs.aggregate(
        total=Count("id"),
        ready=Count("id", filter=Q(status=KnowledgeSource.STATUS_READY)),
        ingesting=Count("id", filter=Q(status=KnowledgeSource.STATUS_INGESTING)),
        pending=Count("id", filter=Q(status=KnowledgeSource.STATUS_PENDING)),
        failed=Count("id", filter=Q(status=KnowledgeSource.STATUS_FAILED)),
    )


class KnowledgeSourceListView(TenantScopedAPIView):
    """GET = list the website's sources. POST = enqueue ingest.

    GET supports filter + pagination query params:
      status   — pending | ingesting | ready | failed
      kind     — homepage | blog | product | docs | review | other | audit_context
      domain   — exact-match hostname filter, or "paste" for text uploads
      search   — substring match on url or title (case-insensitive)
      page, page_size — standard DRF pagination (default page_size=25, max=100)

    A ``stats`` block is included so the UI can render status-count
    badges without a second request. ``stats.chunks_pending`` counts
    chunks stored without a vector, which search cannot reach yet.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        base_qs = KnowledgeSource.objects.filter(website=website)

        # Stats reflect the whole website corpus, not the current
        # filter — badges are for navigation.
        stats = _source_stats(base_qs)
        stats["chunks_pending"] = pending_chunks(website_id=website.id).count()

        qs = base_qs.order_by("-created_at")

        status_param = request.query_params.get("status")
        if status_param in {s for s, _ in KnowledgeSource.STATUS_CHOICES}:
            qs = qs.filter(status=status_param)

        kind_param = request.query_params.get("kind")
        if kind_param in {k for k, _ in KnowledgeSource.KIND_CHOICES}:
            qs = qs.filter(kind=kind_param)

        domain_param = (request.query_params.get("domain") or "").strip().lower()
        if domain_param == "paste":
            qs = qs.filter(url__startswith="paste://")
        elif domain_param:
            # Match rows whose URL starts with ``scheme://<domain>/`` or
            # equals ``scheme://<domain>`` (no trailing slash). Cheaper
            # than parsing every URL in Python.
            qs = qs.filter(
                Q(url__istartswith=f"http://{domain_param}/")
                | Q(url__istartswith=f"https://{domain_param}/")
                | Q(url__iexact=f"http://{domain_param}")
                | Q(url__iexact=f"https://{domain_param}")
            )

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(url__icontains=search) | Q(title__icontains=search))

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = KnowledgeSourceSerializer(page if page is not None else qs, many=True).data

        if page is not None:
            resp = paginator.get_paginated_response(data)
            # StandardPagination returns {count, next, previous, results}.
            # Attach stats onto the same envelope so one round-trip
            # yields both the page and the badge counts.
            resp.data["stats"] = stats
            return resp

        return PageResponse({"results": data, "stats": stats})

    def post(self, request, website_id):
        website = self.get_website(website_id)

        # Per-user throttle. Burst of 10, refills at 30/min. Crawl jobs
        # consume more capacity since they fan out internally.
        serializer = IngestURLSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cost = 5 if data.get("crawl") else 1
        if not _ingest_bucket(request.user.id).try_acquire(cost):
            return Response(
                {"error": "Rate limit exceeded for RAG ingest. "
                          "Slow down and retry shortly."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        from apps.rag.tasks import ingest_site_task, ingest_url_task

        if data.get("crawl"):
            ingest_site_task.delay(
                user_id=request.user.id,
                website_id=str(website.id),
                seed_url=data["url"],
                page_cap=data["page_cap"],
                depth=data["depth"],
            )
            return Response(
                {"queued": True, "mode": "crawl", "seed_url": data["url"]},
                status=status.HTTP_202_ACCEPTED,
            )

        ingest_url_task.delay(
            user_id=request.user.id,
            website_id=str(website.id),
            url=data["url"],
            kind=data["kind"],
            title=data.get("title", ""),
        )
        return Response(
            {"queued": True, "mode": "single", "url": data["url"]},
            status=status.HTTP_202_ACCEPTED,
        )


class KnowledgeSourceDetailView(TenantScopedAPIView):
    """GET / DELETE a single source (and its chunks)."""

    def get(self, request, website_id, source_id):
        website = self.get_website(website_id)
        source = self.get_tenant_object(
            KnowledgeSource.objects.filter(website=website),
            id=source_id,
        )
        chunks = KnowledgeChunk.objects.filter(source=source).order_by("chunk_index")
        return Response({
            "source": KnowledgeSourceSerializer(source).data,
            "chunks": KnowledgeChunkSerializer(chunks, many=True).data,
        })

    def delete(self, request, website_id, source_id):
        website = self.get_website(website_id)
        source = self.get_tenant_object(
            KnowledgeSource.objects.filter(website=website),
            id=source_id,
        )
        source_pk = source.id
        source.delete()

        # Drop the source's vectors from the optional index too. Best
        # effort: Postgres cascade already removed the chunks, and any
        # orphaned vectors are filtered out at query time anyway.
        from apps.rag.services.vector_backends import get_backend

        backend = get_backend()
        if backend is not None:
            try:
                backend.delete_source(website_id=website.id, source_id=source_pk)
            except Exception:
                logger.warning("vector index delete failed", exc_info=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReingestSourceView(TenantScopedAPIView):
    """POST re-queues the URL for a fresh fetch + embed.

    Only URL-backed sources can be reingested. Paste-uploaded text has
    no source we can re-fetch — the user must re-upload to update. The
    task is throttled through the same ingest bucket as the initial add.
    """

    def post(self, request, website_id, source_id):
        website = self.get_website(website_id)
        source = self.get_tenant_object(
            KnowledgeSource.objects.filter(website=website),
            id=source_id,
        )

        if source.url.startswith("paste://"):
            return Response(
                {"error": "Paste-uploaded sources cannot be reingested; "
                          "re-upload the text to update it."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not _ingest_bucket(request.user.id).try_acquire(1):
            return Response(
                {"error": "Rate limit exceeded for RAG ingest. "
                          "Slow down and retry shortly."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Flip status back so the UI shows the row moving through the
        # pipeline again. The task overwrites status when it runs.
        source.status = KnowledgeSource.STATUS_PENDING
        source.error_message = ""
        source.save(update_fields=["status", "error_message", "updated_at"])

        from apps.rag.tasks import ingest_url_task

        ingest_url_task.delay(
            user_id=request.user.id,
            website_id=str(website.id),
            url=source.url,
            kind=source.kind,
            title=source.title,
        )
        return Response(
            {
                "queued": True,
                "source": KnowledgeSourceSerializer(source).data,
                "reingest_requested_at": timezone.now().isoformat(),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class UploadTextView(TenantScopedAPIView):
    """
    Paste raw text or markdown into the knowledge base.

    Reuses the same ingest pipeline as URL ingest by handing the text
    directly to ``ingest_url(text=...)``. The pseudo-URL is derived
    from a SHA-256 of the text so the (website, url) uniqueness
    constraint deduplicates identical pastes across the whole website.
    """

    def post(self, request, website_id):
        website = self.get_website(website_id)

        if not _ingest_bucket(request.user.id).try_acquire(1):
            return Response(
                {"error": "Rate limit exceeded for RAG ingest. "
                          "Slow down and retry shortly."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = UploadTextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Stable pseudo-URL so re-pasting identical content updates the
        # same source instead of creating duplicates.
        digest = hashlib.sha256(data["text"].encode("utf-8")).hexdigest()[:16]
        pseudo_url = f"paste://{website.id}/{digest}"

        result = ingest_url(
            user=request.user, website=website,
            url=pseudo_url,
            kind=data.get("kind") or KnowledgeSource.KIND_OTHER,
            title=data["title"],
            text=data["text"],
        )

        source = KnowledgeSource.objects.get(id=result.source_id)
        return Response(
            {
                "source": KnowledgeSourceSerializer(source).data,
                "chunk_count": result.chunk_count,
                "skipped_unchanged": result.skipped_unchanged,
                "error": result.error,
                # Stored without vectors: search cannot reach this text
                # until embeddings work (reason e.g. "embeddings_not_configured").
                "embeddings_pending": result.embeddings_pending,
                "embedding_reason": result.embedding_reason,
            },
            status=(
                status.HTTP_201_CREATED
                if result.status == KnowledgeSource.STATUS_READY
                else status.HTTP_400_BAD_REQUEST
            ),
        )


class AgentCrawlView(TenantScopedAPIView):
    """Consent switch: may the Cansee agent crawl this site for knowledge?

    GET returns the current state. POST {"enabled": bool} flips it.
    Turning it ON queues a seed crawl of the website's own domain through
    the normal ingest pipeline (unless one was seeded recently), so the
    button does something visible immediately: sources start appearing on
    the Brand Ingestion page.
    """

    SEED_COOLDOWN_HOURS = 24
    SEED_PAGE_CAP = 12
    SEED_DEPTH = 1

    @staticmethod
    def _payload(consent, seeded=False):
        return {
            "enabled": bool(consent and consent.enabled),
            "enabled_at": consent.enabled_at if consent else None,
            "last_seeded_at": consent.last_seeded_at if consent else None,
            "seeded": seeded,
        }

    def get(self, request, website_id):
        website = self.get_website(website_id)
        # Consent is per-website: one teammate granting it covers the
        # project, so reads never filter by user.
        consent = AgentCrawlConsent.objects.filter(website=website).first()
        return Response(self._payload(consent))

    def post(self, request, website_id):
        website = self.get_website(website_id)
        enabled = bool(request.data.get("enabled"))
        consent, _ = AgentCrawlConsent.objects.get_or_create(
            website=website, defaults={"user": request.user},
        )

        seeded = False
        if enabled:
            if not consent.enabled:
                consent.enabled = True
                consent.enabled_at = timezone.now()
            cooled_down = (
                consent.last_seeded_at is None
                or timezone.now() - consent.last_seeded_at
                > timedelta(hours=self.SEED_COOLDOWN_HOURS)
            )
            # Seed crawls share the ingest budget with manual adds; if
            # the bucket is dry the consent still flips — the agent just
            # crawls later instead of now.
            if cooled_down and _ingest_bucket(request.user.id).try_acquire(5):
                from apps.rag.tasks import ingest_site_task

                ingest_site_task.delay(
                    user_id=request.user.id,
                    website_id=str(website.id),
                    seed_url=website.url,
                    page_cap=self.SEED_PAGE_CAP,
                    depth=self.SEED_DEPTH,
                )
                consent.last_seeded_at = timezone.now()
                seeded = True
        else:
            consent.enabled = False
        consent.save()
        return Response(self._payload(consent, seeded=seeded))


class RetrieveView(TenantScopedAPIView):
    """POST a query, get top-k chunks back. Used by the prompt previewer + clients.

    Accepts an optional ``source_id`` to constrain the search to one
    KnowledgeSource — the Brand Input detail drawer uses this so users
    can preview what a specific source would surface for a query.

    ``reason`` is null after a normal search. When the search could not
    run, ``hits`` is empty and ``reason`` says why:
    ``embeddings_not_configured``, ``ai_allowance_exhausted``,
    ``embeddings_failed`` or ``embeddings_pending``.
    """

    def post(self, request, website_id):
        website = self.get_website(website_id)

        # Each retrieval call costs an embedding API call for the query.
        # Throttle to keep that cost bounded per-tenant.
        if not _retrieve_bucket(request.user.id).try_acquire():
            return Response(
                {"error": "Rate limit exceeded for retrieval. "
                          "Slow down and retry shortly."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = RetrieveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source_ids = None
        if data.get("source_id"):
            # Verify the source belongs to this website before letting
            # it constrain retrieval — otherwise a caller could probe
            # existence of other tenants' source_ids by timing.
            source = self.get_tenant_object(
                KnowledgeSource.objects.filter(website=website),
                id=data["source_id"],
            )
            source_ids = [str(source.id)]

        hits = retrieve(
            user=request.user, website=website,
            query=data["query"], top_k=data["top_k"],
            kinds=data.get("kinds") or None,
            source_ids=source_ids,
        )
        return Response({
            "query": data["query"],
            "hits": HitSerializer(hits, many=True).data,
            "reason": hits.reason,
        })
