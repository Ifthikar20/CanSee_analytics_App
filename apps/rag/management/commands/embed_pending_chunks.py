"""Embed knowledge-base chunks that were stored without a vector.

Ingest stores chunks without a vector (marked pending) when embeddings
are unavailable: no OPENAI_API_KEY, a spent AI allowance, or an API
error. Word-hash vectors written by older builds are treated the same
way. Run this once embeddings work; chunk ids are kept, so linked facts
stay linked. Re-running is safe.

Usage:
  python manage.py embed_pending_chunks
  python manage.py embed_pending_chunks --website <uuid>
  python manage.py embed_pending_chunks --dry-run
"""
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.rag.services.embedder import embeddings_configured
from apps.rag.services.ingest_service import embed_pending_chunks, pending_chunks


class Command(BaseCommand):
    help = "Embed knowledge-base chunks that have no vector yet."

    def add_arguments(self, parser):
        parser.add_argument("--website", help="Only this website id.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report how many chunks are pending without embedding them.",
        )

    def handle(self, *args, **opts):
        website_id = opts.get("website")
        if website_id:
            try:
                website_id = uuid.UUID(website_id)
            except ValueError as exc:
                raise CommandError("--website must be a website id (UUID).") from exc

        total = pending_chunks(website_id=website_id).count()
        if not total:
            self.stdout.write("No chunks are waiting for a vector.")
            return
        self.stdout.write(f"{total} chunk(s) have no vector yet.")
        if opts["dry_run"]:
            return
        if not embeddings_configured():
            raise CommandError("OPENAI_API_KEY is not set, so nothing can be embedded yet.")

        result = embed_pending_chunks(website_id=website_id)
        summary = (
            f"Embedded {result.embedded} chunk(s); "
            f"{result.remaining} still have no vector."
        )
        if result.reason:
            raise CommandError(f"{summary} Stopped early: {result.reason}.")
        self.stdout.write(self.style.SUCCESS(summary))
