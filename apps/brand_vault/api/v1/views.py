"""REST endpoints for the Brand Vault."""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brand_vault.api.v1.serializers import (
    BrandFactDetailSerializer,
    BrandFactEditSerializer,
    BrandFactSerializer,
    FactImportItemSerializer,
    FactRevisionSerializer,
)
from apps.brand_vault.models import BrandFact, FactRevision, FactStatus
from apps.brand_vault.services import fact_versioning
from apps.brand_vault.services.embeddings import embed_text
from apps.brand_vault.services.fact_versioning import record_creation
from core.exceptions import ResourceNotFound
from core.views import TenantScopedAPIView, TenantScopedListAPIView

# ── Standard B2B fact coverage ─────────────────────────────────────────────
# Used by WebsiteCoverageView to score how much of the "obvious" stuff a
# brand vault has on file. Predicates are matched case-insensitively with
# a substring check so "was founded" and "founded in" both count for
# the "founded" bucket.
STANDARD_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("founded", ("founded", "established", "started")),
    ("location", ("located", "headquarter", "based in", "office")),
    ("team", ("employee", "team size", "staff", "headcount")),
    ("product", ("offer", "sell", "build", "provide", "specialise", "specialize")),
    ("pricing", ("price", "cost", "subscription", "plan")),
    ("audience", ("serve", "audience", "customer", "target")),
    ("mission", ("mission", "vision", "value", "believe")),
    ("contact", ("contact", "email", "phone", "support")),
]


class WebsiteFactsView(TenantScopedListAPIView):
    """List facts for a website with status / product_line / topic filters."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = BrandFact.objects.filter(website=website)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        product_line = request.query_params.get("product_line")
        if product_line:
            qs = qs.filter(product_line=product_line)

        topic = request.query_params.get("topic")
        if topic:
            qs = qs.filter(topic=topic)

        q = request.query_params.get("q")
        if q:
            qs = qs.filter(
                Q(subject__icontains=q) | Q(predicate__icontains=q)
                | Q(object__icontains=q),
            )

        only_current = request.query_params.get("only_current")
        if only_current and only_current.lower() in ("1", "true", "yes"):
            qs = qs.filter(version_to__isnull=True)

        qs = qs.order_by("-created_at")
        return self.paginated_response(qs, BrandFactSerializer)


def _get_fact_for_user(user, fact_id, *, min_role: str = "member") -> BrandFact:
    from apps.websites.services.website_service import WebsiteService

    try:
        fact = BrandFact.objects.select_related("website").get(id=fact_id)
    except BrandFact.DoesNotExist as exc:
        raise ResourceNotFound("BrandFact not found.") from exc
    WebsiteService.get_for_user(user=user, website_id=fact.website_id, min_role=min_role)
    return fact


class FactDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, fact_id):
        fact = _get_fact_for_user(request.user, fact_id, min_role="viewer")
        return Response(BrandFactDetailSerializer(fact).data)


class FactApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, fact_id):
        fact = _get_fact_for_user(request.user, fact_id)
        fact = fact_versioning.approve_fact(str(fact.id), actor_user=request.user)
        return Response(BrandFactSerializer(fact).data)


class FactRejectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, fact_id):
        fact = _get_fact_for_user(request.user, fact_id)
        fact = fact_versioning.reject_fact(str(fact.id), actor_user=request.user)
        return Response(BrandFactSerializer(fact).data)


class FactEditView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, fact_id):
        fact = _get_fact_for_user(request.user, fact_id)
        ser = BrandFactEditSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_fact = fact_versioning.supersede_fact(
            str(fact.id),
            ser.validated_data["subject"],
            ser.validated_data["predicate"],
            ser.validated_data["object"],
            actor_user=request.user,
        )
        return Response(BrandFactSerializer(new_fact).data, status=status.HTTP_201_CREATED)


class WebsiteExtractView(TenantScopedAPIView):
    """Trigger an LLM extraction pass over the website's KnowledgeChunks."""

    def post(self, request, website_id):
        website = self.get_website(website_id)
        try:
            from apps.brand_vault.tasks import extract_facts_for_website
            extract_facts_for_website.delay(str(website.id))
            queued = True
        except Exception:
            queued = False
        return Response({"queued": queued, "website_id": str(website.id)})


def _import_one(website, item: dict, *, actor_user=None) -> str:
    """Persist one validated fact item. Returns 'created' or 'skipped'."""
    subject = (item.get("subject") or "").strip()
    predicate = (item.get("predicate") or "").strip()
    obj = (item.get("object") or "").strip()
    if not subject or not predicate or not obj:
        raise ValueError("subject, predicate and object are required")
    exists = BrandFact.objects.filter(
        website=website,
        subject__iexact=subject,
        predicate__iexact=predicate,
        object__iexact=obj,
        version_to__isnull=True,
    ).exists()
    if exists:
        return "skipped"
    try:
        confidence = float(item.get("confidence") or 0.9)
    except (TypeError, ValueError):
        confidence = 0.9
    confidence = max(0.0, min(1.0, confidence))
    fact = BrandFact.objects.create(
        website=website,
        subject=subject[:300],
        predicate=predicate[:200],
        object=obj,
        product_line=(item.get("product_line") or "")[:120],
        topic=(item.get("topic") or "")[:120],
        source_url=(item.get("source_url") or "")[:1000],
        confidence=confidence,
        extracted_by="manual",
        status=FactStatus.APPROVED,
    )
    # No vector when embeddings are unavailable; refresh_fact_embeddings
    # fills it in later.
    vector, _model = embed_text(
        f"{subject} {predicate} {obj}", user=actor_user, website=website,
    )
    if vector:
        fact.embedding = vector
        fact.save(update_fields=["embedding", "updated_at"])
    record_creation(fact, actor_user=actor_user)
    return "created"


class WebsiteFactsImportView(TenantScopedAPIView):
    """Bulk-import manually authored facts (JSON body)."""

    parser_classes = [JSONParser]

    def post(self, request, website_id):
        website = self.get_website(website_id)
        raw = request.data.get("facts") if isinstance(request.data, dict) else None
        if not isinstance(raw, list):
            return Response(
                {"detail": "Body must be {\"facts\": [...]}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        created = 0
        skipped = 0
        errors: list[dict] = []
        for idx, item in enumerate(raw):
            ser = FactImportItemSerializer(data=item)
            if not ser.is_valid():
                errors.append({"index": idx, "errors": ser.errors})
                continue
            try:
                outcome = _import_one(website, ser.validated_data, actor_user=request.user)
            except Exception as exc:
                errors.append({"index": idx, "error": str(exc)})
                continue
            if outcome == "created":
                created += 1
            else:
                skipped += 1
        return Response({"created": created, "skipped": skipped, "errors": errors})


class WebsiteFactsImportCSVView(TenantScopedAPIView):
    """Bulk-import facts from a CSV file (multipart/form-data)."""

    parser_classes = [MultiPartParser, FormParser]

    EXPECTED_COLUMNS = (
        "subject", "predicate", "object",
        "product_line", "topic", "confidence", "source_url",
    )

    def post(self, request, website_id):
        website = self.get_website(website_id)
        f = request.FILES.get("file") or request.FILES.get("csv")
        if f is None:
            return Response(
                {"detail": "Upload a 'file' field with a CSV body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            raw = f.read().decode("utf-8-sig", errors="replace")
        except Exception:
            return Response(
                {"detail": "Could not decode CSV as UTF-8."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reader = csv.DictReader(io.StringIO(raw))
        created = 0
        skipped = 0
        errors: list[dict] = []
        for idx, row in enumerate(reader):
            data = {k: (row.get(k) or "").strip() for k in self.EXPECTED_COLUMNS}
            if data.get("confidence") in ("", None):
                data.pop("confidence", None)
            ser = FactImportItemSerializer(data=data)
            if not ser.is_valid():
                errors.append({"row": idx + 1, "errors": ser.errors})
                continue
            try:
                outcome = _import_one(website, ser.validated_data, actor_user=request.user)
            except Exception as exc:
                errors.append({"row": idx + 1, "error": str(exc)})
                continue
            if outcome == "created":
                created += 1
            else:
                skipped += 1
        return Response({"created": created, "skipped": skipped, "errors": errors})


class WebsiteStatsView(TenantScopedAPIView):
    """Aggregate counts for the dashboard header."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = BrandFact.objects.filter(website=website)

        by_status: Counter = Counter()
        by_product: Counter = Counter()
        by_topic: Counter = Counter()
        for s, pl, tp in qs.values_list("status", "product_line", "topic"):
            by_status[s] += 1
            if pl:
                by_product[pl] += 1
            if tp:
                by_topic[tp] += 1

        recent = qs.order_by("-created_at")[:5]
        recent_data = BrandFactSerializer(recent, many=True).data

        # Stale facts — anything approved/auto more than 180 days ago.
        stale_cutoff = timezone.now() - timezone.timedelta(days=180)
        stale = qs.filter(
            status__in=[FactStatus.APPROVED, FactStatus.AUTO],
            version_to__isnull=True,
            version_from__lt=stale_cutoff,
        ).count()

        # Last extraction timestamp — surface to the header so the user
        # knows when we last refreshed the vault.
        last = qs.order_by("-created_at").values_list("created_at", flat=True).first()

        return Response({
            "website_id": str(website.id),
            "total": sum(by_status.values()),
            "by_status": {
                FactStatus.PENDING.value: by_status.get(FactStatus.PENDING.value, 0),
                FactStatus.APPROVED.value: by_status.get(FactStatus.APPROVED.value, 0),
                FactStatus.REJECTED.value: by_status.get(FactStatus.REJECTED.value, 0),
                FactStatus.AUTO.value: by_status.get(FactStatus.AUTO.value, 0),
            },
            "by_product_line": dict(by_product.most_common(20)),
            "by_topic": dict(by_topic.most_common(20)),
            "stale_count": stale,
            "last_extracted_at": last.isoformat() if last else None,
            "recent": recent_data,
        })


class WebsiteBulkFactActionView(TenantScopedAPIView):
    """
    Batch-approve or batch-reject a list of fact ids.

    Body: ``{"ids": ["uuid", ...], "action": "approve"|"reject"}``.
    Skips ids that don't belong to this website and reports per-id
    outcomes so the UI can highlight any that failed.
    """

    def post(self, request, website_id):
        website = self.get_website(website_id)
        ids = request.data.get("ids") or []
        action = (request.data.get("action") or "").lower()
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "ids must be a non-empty array."},
                            status=status.HTTP_400_BAD_REQUEST)
        if action not in ("approve", "reject"):
            return Response({"detail": "action must be 'approve' or 'reject'."},
                            status=status.HTTP_400_BAD_REQUEST)

        facts = list(BrandFact.objects.filter(website=website, id__in=ids))
        approved = []
        rejected = []
        errors = []
        for fact in facts:
            try:
                if action == "approve":
                    f = fact_versioning.approve_fact(str(fact.id), actor_user=request.user)
                    approved.append(str(f.id))
                else:
                    f = fact_versioning.reject_fact(str(fact.id), actor_user=request.user)
                    rejected.append(str(f.id))
            except Exception as exc:
                errors.append({"id": str(fact.id), "error": str(exc)})

        missing = sorted(set(map(str, ids)) - {str(f.id) for f in facts})
        return Response({
            "approved_ids": approved,
            "rejected_ids": rejected,
            "missing_ids": missing,
            "errors": errors,
        })


class FactUsageView(APIView):
    """
    Activity around one fact.

    ``audit_count`` counts audits on the fact's website that completed
    after the fact's version_from: the fact was on record when they ran.
    Nothing records whether an individual answer used it, so this is
    not a citation count. ``revision_count`` is exact.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, fact_id):
        from apps.llm_ranking.models import LLMRankingAudit

        fact = _get_fact_for_user(request.user, fact_id, min_role="viewer")
        audit_count = LLMRankingAudit.objects.filter(
            website_id=fact.website_id,
            status=LLMRankingAudit.STATUS_COMPLETED,
            completed_at__gte=fact.version_from,
        ).count()
        return Response({
            "fact_id": str(fact.id),
            "audit_count": audit_count,
            "revision_count": fact.revisions.count(),
            "since": fact.version_from.isoformat() if fact.version_from else None,
        })


class WebsiteContradictionsView(TenantScopedAPIView):
    """
    Surface facts that contradict each other — same subject + predicate,
    different object, both currently in force (approved/auto, no
    version_to). Critical for trust because downstream agents can be
    handed conflicting grounding.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = BrandFact.objects.filter(
            website=website,
            status__in=[FactStatus.APPROVED, FactStatus.AUTO],
            version_to__isnull=True,
        )
        grouped: dict[tuple[str, str], list[BrandFact]] = defaultdict(list)
        for f in qs:
            key = (f.subject.strip().lower(), f.predicate.strip().lower())
            grouped[key].append(f)

        clusters = []
        for (_subject, _predicate), facts in grouped.items():
            if len(facts) < 2:
                continue
            objects = {f.object.strip().lower() for f in facts}
            if len(objects) < 2:
                continue
            clusters.append({
                "subject": facts[0].subject,
                "predicate": facts[0].predicate,
                "fact_count": len(facts),
                "facts": BrandFactSerializer(facts, many=True).data,
            })

        clusters.sort(key=lambda c: -c["fact_count"])
        return Response({
            "website_id": str(website.id),
            "cluster_count": len(clusters),
            "clusters": clusters,
        })


class WebsiteCoverageView(TenantScopedAPIView):
    """
    Score how complete the vault is against a standard B2B fact
    taxonomy (founded, location, team, product, pricing, audience,
    mission, contact). Each bucket counts approved/auto current facts
    whose predicate matches any of the bucket's keywords.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = BrandFact.objects.filter(
            website=website,
            status__in=[FactStatus.APPROVED, FactStatus.AUTO],
            version_to__isnull=True,
        ).values_list("predicate", flat=True)

        predicates = [p.lower() for p in qs]
        buckets = []
        covered = 0
        for key, needles in STANDARD_BUCKETS:
            count = sum(1 for p in predicates if any(n in p for n in needles))
            if count > 0:
                covered += 1
            buckets.append({"key": key, "count": count, "covered": count > 0})

        score = round(100 * covered / len(STANDARD_BUCKETS)) if STANDARD_BUCKETS else 0
        return Response({
            "website_id": str(website.id),
            "score": score,
            "covered_buckets": covered,
            "total_buckets": len(STANDARD_BUCKETS),
            "buckets": buckets,
        })


class WebsiteRevisionsView(TenantScopedListAPIView):
    """Recent FactRevision rows for the History tab."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = FactRevision.objects.filter(
            fact__website=website,
        ).select_related("actor_user", "fact").order_by("-created_at")
        action = request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)
        return self.paginated_response(qs, FactRevisionSerializer)


class WebsiteExtractStatusView(TenantScopedAPIView):
    """
    Poll target for the extraction progress banner.

    We don't have per-task progress in Redis (the task just writes
    facts as it finds them), so the status proxy is the count of
    facts created in the last 10 minutes. Good enough for the UI to
    show "N new facts since you clicked Re-scan".
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        since = timezone.now() - timezone.timedelta(minutes=10)
        recent = BrandFact.objects.filter(
            website=website,
            created_at__gte=since,
        ).count()
        last = BrandFact.objects.filter(
            website=website,
        ).order_by("-created_at").values_list("created_at", flat=True).first()
        return Response({
            "website_id": str(website.id),
            "recent_facts": recent,
            "last_extracted_at": last.isoformat() if last else None,
        })


class WebsiteFactCheckView(TenantScopedAPIView):
    """
    Compare a piece of free text (typically an LLM response) against
    the website's approved + auto current facts.

    Body: ``{"text": "..."}``

    For each fact we classify the response as:
      - corroborated: object substring is present (case-insensitive)
      - contradicted: subject + predicate substrings both present but
        object missing (the model is talking about the same attribute
        but with a different value)
      - missed:       neither subject nor object substring is present
      - implicit:     subject substring present, object not present,
                      and the predicate isn't either — not used as a
                      contradiction but worth flagging that the
                      response touches the entity at all

    The classification is intentionally conservative — the goal is to
    surface gaps for the user to scan visually, not to score the
    model. A fact is matched against the lowercased response with
    naive substring containment; in practice that catches roughly all
    direct mentions and avoids false positives from word fragments
    because subjects/objects in BrandFact are usually multi-word.
    """

    parser_classes = [JSONParser]

    def post(self, request, website_id):
        website = self.get_website(website_id)
        text = (request.data.get("text") or "")
        if not text:
            return Response({
                "website_id": str(website.id),
                "corroborated": [],
                "contradicted": [],
                "missed": [],
                "total": 0,
            })

        lowered = text.lower()
        facts = list(BrandFact.objects.filter(
            website=website,
            status__in=[FactStatus.APPROVED, FactStatus.AUTO],
            version_to__isnull=True,
        ))

        corroborated: list[dict] = []
        contradicted: list[dict] = []
        missed: list[dict] = []

        for f in facts:
            subj = (f.subject or "").strip().lower()
            pred = (f.predicate or "").strip().lower()
            obj = (f.object or "").strip().lower()
            if not obj:
                continue
            row = {
                "id": str(f.id),
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "topic": f.topic or "",
                "confidence": f.confidence,
            }
            obj_hit = obj in lowered
            subj_hit = bool(subj) and subj in lowered
            pred_hit = bool(pred) and pred in lowered
            if obj_hit:
                corroborated.append(row)
            elif subj_hit and pred_hit:
                # The response is talking about the same attribute of the
                # same entity but with a different value — flag it.
                contradicted.append(row)
            elif subj_hit or pred_hit:
                # Touches the entity / attribute but doesn't say the
                # canonical value. Bucket with missed so the user sees
                # it as "model didn't surface this".
                missed.append(row)
            else:
                missed.append(row)

        # Cap missed at 50 so we don't dump every fact in the vault on
        # a response that says nothing relevant.
        missed = missed[:50]

        return Response({
            "website_id": str(website.id),
            "corroborated": corroborated,
            "contradicted": contradicted,
            "missed": missed,
            "total": len(facts),
        })


class WebsiteKeywordsView(TenantScopedAPIView):
    """
    Aggregate keyword / topic repository for the Brand Vault widget.

    For every distinct ``topic`` value across this website's approved
    + auto current facts we return a row with mention_count (facts in
    that topic), avg_confidence, first/last_seen, share_pct, and an
    8-bucket timeline (fact creations per week) that the UI renders
    as a sparkline. Topics with zero current facts are excluded.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = BrandFact.objects.filter(
            website=website,
            status__in=[FactStatus.APPROVED, FactStatus.AUTO],
            version_to__isnull=True,
        ).exclude(topic="")

        rows = list(qs.values("topic", "confidence", "created_at", "product_line"))
        if not rows:
            return Response({
                "website_id": str(website.id),
                "items": [],
                "total_mentions": 0,
            })

        total = len(rows)

        # 8 weekly buckets ending now — gives the sparkline a stable
        # length regardless of how thin a topic is.
        now = timezone.now()
        bucket_count = 8

        groups: dict[str, dict] = {}
        for r in rows:
            topic = (r["topic"] or "").strip()
            if not topic:
                continue
            entry = groups.setdefault(topic, {
                "topic": topic,
                "mention_count": 0,
                "_conf_sum": 0.0,
                "first_seen": None,
                "last_seen": None,
                "product_lines": set(),
                "timeline": [0] * bucket_count,
            })
            entry["mention_count"] += 1
            entry["_conf_sum"] += float(r["confidence"] or 0)
            ts = r["created_at"]
            if entry["first_seen"] is None or ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if entry["last_seen"] is None or ts > entry["last_seen"]:
                entry["last_seen"] = ts
            if r["product_line"]:
                entry["product_lines"].add(r["product_line"])
            age_days = (now - ts).days
            bucket_idx = bucket_count - 1 - min(bucket_count - 1, age_days // 7)
            if bucket_idx >= 0:
                entry["timeline"][bucket_idx] += 1

        items = []
        for entry in groups.values():
            mc = entry["mention_count"] or 1
            items.append({
                "topic": entry["topic"],
                "mention_count": entry["mention_count"],
                "avg_confidence": round(entry["_conf_sum"] / mc, 3),
                "first_seen": entry["first_seen"].isoformat() if entry["first_seen"] else None,
                "last_seen": entry["last_seen"].isoformat() if entry["last_seen"] else None,
                "share_pct": round(100 * entry["mention_count"] / total, 1),
                "product_lines": sorted(entry["product_lines"]),
                "timeline": entry["timeline"],
            })

        items.sort(key=lambda e: (-e["mention_count"], e["topic"]))
        return Response({
            "website_id": str(website.id),
            "items": items,
            "total_mentions": total,
        })


class WebsiteBacklinksView(TenantScopedAPIView):
    """
    "Where was your brand mentioned" — aggregate of third-party domains
    cited by LLM models in responses that named your brand.

    A backlink here means: a citation URL that appeared inside an
    LLMRankingResult whose ``is_mentioned`` flag is True, that does
    NOT point at your own site (``is_target=False``). One row per
    apex_domain with first/last-seen timestamps and a recency flag so
    the UI can pill the freshly-detected ones.
    """

    def get(self, request, website_id):
        website = self.get_website(website_id)
        try:
            from apps.citations.models import Citation
        except Exception:
            return Response({"website_id": str(website.id), "items": [], "total_mentions": 0})

        cutoff_recent = timezone.now() - timezone.timedelta(days=7)
        rows = (
            Citation.objects.filter(
                audit__website=website,
                result__is_mentioned=True,
                is_target=False,
            )
            .values(
                "apex_domain", "domain", "source_class",
                "is_competitor", "title",
            )
            .order_by()
        )
        # Aggregate per apex_domain in Python — the dataset is small
        # per website (hundreds of citations max) and Postgres-side
        # MIN/MAX with the same group-by is more brittle across the
        # multiple title/domain variants. We materialise the queryset
        # once and walk it; collect first/last seen separately.
        rows = list(rows)
        if not rows:
            return Response({"website_id": str(website.id), "items": [], "total_mentions": 0})

        # Need created_at too — re-query lightweight.
        ts_rows = list(
            Citation.objects.filter(
                audit__website=website,
                result__is_mentioned=True,
                is_target=False,
            ).values("apex_domain", "created_at"),
        )

        bucket: dict[str, dict] = {}
        for r in rows:
            apex = r["apex_domain"] or r["domain"]
            if not apex:
                continue
            entry = bucket.setdefault(apex, {
                "apex_domain": apex,
                "domain": r["domain"],
                "source_class": r["source_class"],
                "is_competitor_source": False,
                "title": r.get("title") or "",
                "mention_count": 0,
                "first_seen": None,
                "last_seen": None,
                "recent": False,
            })
            entry["mention_count"] += 1
            if r["is_competitor"]:
                entry["is_competitor_source"] = True

        for r in ts_rows:
            apex = r["apex_domain"]
            if not apex or apex not in bucket:
                continue
            ts = r["created_at"]
            entry = bucket[apex]
            if entry["first_seen"] is None or ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if entry["last_seen"] is None or ts > entry["last_seen"]:
                entry["last_seen"] = ts

        items = []
        for entry in bucket.values():
            entry["recent"] = bool(entry["last_seen"] and entry["last_seen"] >= cutoff_recent)
            entry["first_seen"] = entry["first_seen"].isoformat() if entry["first_seen"] else None
            entry["last_seen"] = entry["last_seen"].isoformat() if entry["last_seen"] else None
            items.append(entry)

        items.sort(key=lambda e: (-e["mention_count"], e["apex_domain"]))
        return Response({
            "website_id": str(website.id),
            "items": items[:50],
            "total_mentions": sum(e["mention_count"] for e in items),
            "unique_domains": len(items),
        })
