"""Extract (or re-extract) security claims from stored probe answers.

Replays the security extraction over ``LLMRankingResult`` rows of
security-probe audits that were never extracted or carry an older
extraction version, then scores claim support. Uses the Haiku extractor,
so it spends tenant AI budget; ``--dry-run`` reports the row count first.

    python manage.py backfill_security_claims [--website <uuid>] [--limit 500]
                                              [--dry-run] [--rescore-only]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.llm_ranking.services.extraction_service import (
    SECURITY_EXTRACTION_VERSION,
    SecurityClaimExtractionService,
)


class Command(BaseCommand):
    help = "Extract security claims from stored security-probe answers and score their support."

    def add_arguments(self, parser):
        parser.add_argument("--website", default="", help="Limit to one website id.")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report how many rows are due without calling any model.",
        )
        parser.add_argument(
            "--rescore-only", action="store_true",
            help="Skip extraction; only re-run claim support scoring on rows that have claims.",
        )

    def handle(self, *args, **options):
        from apps.brand_vault.services.security.perception import score_claim_support

        qs = (
            LLMRankingResult.objects
            .filter(
                audit__probe_kind=LLMRankingAudit.PROBE_KIND_SECURITY,
                query_succeeded=True,
            )
            .exclude(response_text="")
            .select_related("audit__website", "audit__created_by")
            .order_by("created_at")
        )
        if options["website"]:
            qs = qs.filter(audit__website_id=options["website"])
        if options["rescore_only"]:
            qs = qs.filter(security_claims__has_key="claims")
        else:
            qs = qs.filter(
                Q(security_claims={})
                | ~Q(security_claims__version=SECURITY_EXTRACTION_VERSION)
            )
        rows = list(qs[: options["limit"]])

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"{len(rows)} row(s) due"))
            return

        extracted = scored = failed = 0
        for result in rows:
            audit = result.audit
            try:
                if not options["rescore_only"]:
                    result.security_claims = SecurityClaimExtractionService.extract(
                        response_text=result.response_text,
                        brand_name=audit.business_name,
                        keywords=audit.keywords,
                        user=audit.created_by,
                        website=audit.website,
                        audit_id=str(audit.id),
                    )
                    result.save(update_fields=["security_claims", "updated_at"])
                    extracted += 1
                if (result.security_claims or {}).get("claims"):
                    score_claim_support(result)
                    scored += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f"failed for {result.id}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"processed {len(rows)} row(s): extracted={extracted} scored={scored} failed={failed}",
        ))
