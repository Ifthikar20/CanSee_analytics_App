"""Rebuild the RAG vector index from Postgres.

Postgres is the source of truth for every KnowledgeChunk; the vector
index is derived. This command makes that promise real: it walks every
source that has chunks and mirrors them into the configured backend,
exactly as ingest_service does at write time.

Run it once when turning RAG_VECTOR_BACKEND on for an existing
installation - without a backfill the index is empty and retrieval
falls through to the Python path (safe, but the index does nothing).
Also the recovery tool for a lost or corrupted index.

Usage:
  python manage.py rag_reindex             # everything
  python manage.py rag_reindex --website <uuid>
  python manage.py rag_reindex --dry-run
"""
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.rag.models import KnowledgeChunk
from apps.rag.services.embedder import UNEMBEDDED_MODEL_NAMES
from apps.rag.services.vector_backends import get_backend


class Command(BaseCommand):
    help = "Mirror every model-embedded KnowledgeChunk into the vector index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--website", help="Reindex a single website id only.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be indexed without writing.",
        )

    def handle(self, *args, **opts):
        backend = get_backend()
        if backend is None:
            raise CommandError(
                "No vector backend configured. Set RAG_VECTOR_BACKEND=chroma "
                "(and RAG_CHROMA_URL for server mode) before reindexing."
            )

        # Only model vectors are indexed: pending chunks have none, and
        # legacy word-hash vectors are not semantic.
        qs = KnowledgeChunk.objects.exclude(embedding_model__in=UNEMBEDDED_MODEL_NAMES)
        if opts["website"]:
            qs = qs.filter(website_id=opts["website"])

        # Group chunks per (website, source, dim) - replace_source is the
        # same primitive the ingest hook uses, so reindexing converges to
        # the identical state a fresh ingest would produce.
        groups = defaultdict(list)
        for chunk in qs.only(
            "id", "embedding", "embedding_dim", "website_id", "source_id",
        ).iterator(chunk_size=500):
            if not chunk.embedding:
                continue
            key = (chunk.website_id, chunk.source_id, len(chunk.embedding))
            groups[key].append(chunk)

        if not groups:
            self.stdout.write("Nothing to index.")
            return

        websites = {k[0] for k in groups}
        total = sum(len(v) for v in groups.values())
        self.stdout.write(
            f"{total} chunks across {len(groups)} sources "
            f"on {len(websites)} website(s)."
        )
        if opts["dry_run"]:
            return

        done = failed = 0
        for (website_id, source_id, dim), chunks in groups.items():
            try:
                backend.replace_source(
                    website_id=website_id, source_id=source_id,
                    chunk_ids=[c.id for c in chunks],
                    vectors=[c.embedding for c in chunks],
                    dim=dim,
                )
                done += len(chunks)
            except Exception as exc:
                failed += len(chunks)
                self.stderr.write(
                    f"  source {source_id} failed: {exc}"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Indexed {done} chunks" + (f", {failed} FAILED" if failed else ".")
        ))
        if failed:
            raise CommandError("Some sources failed; index is partial.")
