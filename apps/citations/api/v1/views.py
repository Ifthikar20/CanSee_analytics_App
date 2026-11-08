"""REST endpoints for citations and source-influence rollups.

All website-scoped endpoints inherit :class:`TenantScopedAPIView` so the
caller must own (or have access to) the website. Audit-scoped endpoints
go through the same gate by resolving the audit's website first.
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.citations.api.v1.serializers import (
    CitationSerializer,
    SourceInfluenceSnapshotSerializer,
)
from apps.citations.models import (
    Citation,
    SourceClass,
    SourceInfluenceSnapshot,
)
from apps.citations.services.snapshot_service import compute_audit_breakdown
from apps.llm_ranking.models import LLMRankingAudit
from core.views import TenantScopedAPIView, TenantScopedListAPIView


def _get_audit_for_user(user, audit_id, *, min_role: str = "member"):
    """Return the audit if ``user`` can reach its website, else 404/403."""
    from apps.websites.services.website_service import WebsiteService

    audit = get_object_or_404(LLMRankingAudit, id=audit_id)
    WebsiteService.get_for_user(user=user, website_id=audit.website_id, min_role=min_role)
    return audit


class AuditCitationsView(TenantScopedListAPIView):
    """List citations for one audit. Filterable by provider + source_class."""

    def get(self, request, audit_id):
        audit = _get_audit_for_user(request.user, audit_id, min_role="viewer")
        qs = Citation.objects.filter(audit=audit).select_related("result")

        provider = request.query_params.get("provider")
        if provider:
            qs = qs.filter(result__provider=provider)

        source_class = request.query_params.get("source_class")
        if source_class:
            qs = qs.filter(source_class=source_class)

        qs = qs.order_by("position", "id")
        return self.paginated_response(qs, CitationSerializer)


class AuditSourceInfluenceView(APIView):
    """Live (non-persisted) breakdown for one audit."""

    permission_classes = [IsAuthenticated]

    def get(self, request, audit_id):
        audit = _get_audit_for_user(request.user, audit_id, min_role="viewer")
        return Response(compute_audit_breakdown(audit))


class WebsiteSourceInfluenceView(TenantScopedAPIView):
    """Per-website rollup over the trailing window.

    Returns a fully aggregated payload (totals, breakdown, top_domains,
    your_site_share, competitor_share, by_provider). Raw snapshots are
    only included when ``?include_raw=true`` is passed; the field is
    always present (possibly empty list) for backwards compatibility.
    """

    def get(self, request, website_id):
        from collections import Counter

        website = self.get_website(website_id)
        period_days = int(request.query_params.get("period_days", 30) or 30)
        provider = request.query_params.get("provider")
        include_raw = request.query_params.get("include_raw", "").lower() in (
            "1", "true", "yes",
        )

        end = timezone.now().date()
        start = end - timedelta(days=period_days)

        # Live citation rollup (always fresh).
        cit_qs = Citation.objects.filter(
            audit__website=website,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).select_related("result").only(
            "source_class", "apex_domain", "is_target", "is_competitor",
            "result__provider",
        )
        if provider:
            cit_qs = cit_qs.filter(result__provider=provider)

        cls_counter: Counter = Counter()
        domain_counter: Counter = Counter()
        domain_class_counter: dict[str, Counter] = {}
        domain_target: dict[str, int] = {}
        domain_competitor: dict[str, int] = {}
        per_provider: dict[str, dict] = {}
        your_site_count = 0
        competitor_count = 0
        total = 0

        for row in cit_qs:
            total += 1
            cls_counter[row.source_class] += 1
            if row.is_target:
                your_site_count += 1
            if row.is_competitor:
                competitor_count += 1
            d = row.apex_domain
            if d:
                domain_counter[d] += 1
                domain_class_counter.setdefault(d, Counter())[row.source_class] += 1
                if row.is_target:
                    domain_target[d] = domain_target.get(d, 0) + 1
                if row.is_competitor:
                    domain_competitor[d] = domain_competitor.get(d, 0) + 1
            prov = row.result.provider
            pp = per_provider.setdefault(prov, {
                "total_citations": 0,
                "_cls": Counter(),
                "_domains": Counter(),
                "_domain_cls": {},
                "_domain_target": {},
                "_domain_competitor": {},
            })
            pp["total_citations"] += 1
            pp["_cls"][row.source_class] += 1
            if d:
                pp["_domains"][d] += 1
                pp["_domain_cls"].setdefault(d, Counter())[row.source_class] += 1
                if row.is_target:
                    pp["_domain_target"][d] = pp["_domain_target"].get(d, 0) + 1
                if row.is_competitor:
                    pp["_domain_competitor"][d] = pp["_domain_competitor"].get(d, 0) + 1

        breakdown: dict = {}
        if total:
            for cls, count in cls_counter.items():
                breakdown[cls] = {
                    "count": count,
                    "share": round(count / total, 4),
                }

        top_domains = []
        for d, c in domain_counter.most_common(50):
            cls_for_domain = "other"
            if d in domain_class_counter and domain_class_counter[d]:
                cls_for_domain = domain_class_counter[d].most_common(1)[0][0]
            top_domains.append({
                "apex_domain": d,
                "count": c,
                "share": round(c / total, 4) if total else 0,
                "source_class": cls_for_domain,
                "is_target": domain_target.get(d, 0) > 0,
                "is_competitor": domain_competitor.get(d, 0) > 0,
            })

        by_provider: dict[str, dict] = {}
        for prov, pp in per_provider.items():
            ptotal = pp["total_citations"]
            pbreakdown: dict = {}
            for cls, count in pp["_cls"].items():
                pbreakdown[cls] = {
                    "count": count,
                    "share": round(count / ptotal, 4) if ptotal else 0,
                }
            ptop = []
            for d, c in pp["_domains"].most_common(50):
                cls_for_d = "other"
                if d in pp["_domain_cls"] and pp["_domain_cls"][d]:
                    cls_for_d = pp["_domain_cls"][d].most_common(1)[0][0]
                ptop.append({
                    "apex_domain": d,
                    "count": c,
                    "share": round(c / ptotal, 4) if ptotal else 0,
                    "source_class": cls_for_d,
                    "is_target": pp["_domain_target"].get(d, 0) > 0,
                    "is_competitor": pp["_domain_competitor"].get(d, 0) > 0,
                })
            by_provider[prov] = {
                "total_citations": ptotal,
                "breakdown": pbreakdown,
                "top_domains": ptop,
            }

        # Snapshots (kept for backwards compat).
        snap_qs = SourceInfluenceSnapshot.objects.filter(
            website=website,
            period_end__gte=start,
        ).order_by("-period_end")
        if provider:
            snap_qs = snap_qs.filter(provider=provider)
        snapshots_data = (
            SourceInfluenceSnapshotSerializer(snap_qs, many=True).data
            if include_raw
            else []
        )

        return Response({
            "website_id": str(website.id),
            "period_days": period_days,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "total_citations": total,
            "unique_domains": len(domain_counter),
            "your_site_share": round(your_site_count / total, 4) if total else 0,
            "competitor_share": round(competitor_count / total, 4) if total else 0,
            "breakdown": breakdown,
            "top_domains": top_domains,
            "by_provider": by_provider,
            "snapshots": snapshots_data,
        })


class WebsiteCitationsView(TenantScopedListAPIView):
    """Paginated raw citations across every audit on a website."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = Citation.objects.filter(audit__website=website).select_related("result")

        provider = request.query_params.get("provider")
        if provider:
            qs = qs.filter(result__provider=provider)

        source_class = request.query_params.get("source_class")
        if source_class:
            qs = qs.filter(source_class=source_class)

        since = request.query_params.get("since")
        if since:
            qs = qs.filter(created_at__date__gte=since)

        qs = qs.order_by("-created_at")
        return self.paginated_response(qs, CitationSerializer)


class WebsiteUrlsView(TenantScopedAPIView):
    """Sources > URLs list view: overview chart, movers, types, and URL rows.

    Aggregates every cited URL across the website's audits within the window.
    """

    def get(self, request, website_id):
        from apps.citations.services.traffic_enrichment import enrich_urls_payload
        from apps.citations.services.url_analytics import build_urls_overview
        from apps.citations.services.url_opportunities import build_opportunities

        website = self.get_website(website_id)
        period_days = int(request.query_params.get("period_days", 30) or 30)
        provider = request.query_params.get("provider") or None
        topic = request.query_params.get("topic") or None

        end = timezone.now().date()
        start = end - timedelta(days=period_days)
        payload = build_urls_overview(
            website, start=start, end=end, provider=provider, topic=topic
        )
        # Real-traffic join (GSC clicks + AI-referred visits for the
        # user's own cited pages) and the derived action buckets. Both
        # read the enriched rows, so order matters.
        enrich_urls_payload(website, payload, start=start, end=end)
        payload["opportunities"] = build_opportunities(payload)
        return Response(payload)


class WebsiteUrlDetailView(TenantScopedAPIView):
    """Sources > URLs detail view for a single cited URL.

    The URL is identified by its ``normalized_url`` passed as ``?url=``.
    """

    def get(self, request, website_id):
        from apps.citations.services.url_analytics import build_url_detail
        from apps.citations.services.url_normalizer import normalize_url

        website = self.get_website(website_id)
        raw = request.query_params.get("url")
        if not raw:
            return Response({"detail": "Missing 'url' query parameter."}, status=400)
        normalized, _host, _apex = normalize_url(raw)

        period_days = int(request.query_params.get("period_days", 30) or 30)
        provider = request.query_params.get("provider") or None
        topic = request.query_params.get("topic") or None
        end = timezone.now().date()
        start = end - timedelta(days=period_days)

        payload = build_url_detail(
            website, normalized_url=normalized, start=start, end=end,
            provider=provider, topic=topic,
        )
        if payload is None:
            return Response({"detail": "URL not found for this website."}, status=404)

        from apps.citations.services.traffic_enrichment import enrich_url_detail
        from apps.citations.services.url_analytics import _prev_window

        prev_start, prev_end = _prev_window(start, end)
        metrics = payload.get("metrics") or {}
        payload["real_traffic"] = enrich_url_detail(
            website, normalized,
            start=start, end=end, prev_start=prev_start, prev_end=prev_end,
            is_target=bool(metrics.get("is_target")),
        )
        return Response(payload)


class WebsiteChatDetailView(TenantScopedAPIView):
    """Full conversation detail for one chat (LLMRankingResult) on a website."""

    def get(self, request, website_id, result_id):
        from apps.citations.services.url_analytics import build_chat_detail

        website = self.get_website(website_id)
        payload = build_chat_detail(website, result_id=result_id)
        if payload is None:
            return Response({"detail": "Chat not found for this website."}, status=404)
        return Response(payload)


class WebsiteChatAttributionView(TenantScopedAPIView):
    """Where one AI answer most likely got its information
    (docs/attribution_contract.md section 1).

    GET returns what is stored and never starts work; POST starts the
    analysis, or re-runs it with ``{"refresh": true}``.
    """

    def get(self, request, website_id, result_id):
        from apps.citations.services.attribution import get_payload

        website = self.get_website(website_id)
        data = get_payload(website, result_id)
        if data is None:
            return Response({"detail": "Chat not found for this website."}, status=404)
        return Response(data)

    def post(self, request, website_id, result_id):
        from apps.citations.services.attribution import start

        website = self.get_website(website_id)
        refresh = request.data.get("refresh") is True if hasattr(request.data, "get") else False
        data = start(website, result_id, user=request.user, refresh=refresh)
        if data is None:
            return Response({"detail": "Chat not found for this website."}, status=404)
        return Response(data, status=202 if data["status"] == "pending" else 200)


class WebsitePageSnapshotView(TenantScopedAPIView):
    """The captured reader view of a page one of this website's
    attributions points at (docs/attribution_contract.md section 2)."""

    def get(self, request, website_id):
        from apps.citations.services.attribution import snapshot_payload

        website = self.get_website(website_id)
        data = snapshot_payload(website, request.query_params.get("url", ""))
        if data is None:
            return Response({"detail": "No snapshot for this page."}, status=404)
        return Response(data)


class GlobalSourceInfluenceView(APIView):
    """Global (cross-tenant) rollup for benchmarking. No PII; safe to expose."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = request.query_params.get("provider")
        industry_id = request.query_params.get("industry")
        period_days = int(request.query_params.get("period_days", 30) or 30)

        end = timezone.now().date()
        start = end - timedelta(days=period_days)

        qs = SourceInfluenceSnapshot.objects.filter(
            website__isnull=True,
            period_end__gte=start,
        ).order_by("-period_end")
        if provider:
            qs = qs.filter(provider=provider)
        if industry_id:
            qs = qs.filter(industry_id=industry_id)

        return Response({
            "period_days": period_days,
            "snapshots": SourceInfluenceSnapshotSerializer(qs, many=True).data,
            "source_classes": [
                {"value": v, "label": label} for v, label in SourceClass.choices
            ],
        })


class SourceScanListCreateView(TenantScopedAPIView):
    """Source Intelligence scans: list recent, or start a new one.

    POST body: {"query": "best bagels in dallas"}. The scan runs
    asynchronously; poll the detail endpoint until status is complete.
    """

    MAX_ACTIVE_PER_WEBSITE = 3

    def get(self, request, website_id):
        from apps.citations.models import SourceScan
        from apps.citations.services import web_search

        website = self.get_website(website_id)
        scans = SourceScan.objects.filter(website=website)[:20]
        return Response({
            "scans": [_scan_summary(s) for s in scans],
            # Lets the UI show a "search key not configured" notice
            # without having to attempt a POST first.
            "configured": web_search.is_configured(),
        })

    def post(self, request, website_id):
        from apps.citations.models import SourceScan, SourceScanStatus
        from apps.citations.services import web_search
        from apps.citations.tasks import run_source_scan

        website = self.get_website(website_id)
        if not web_search.is_configured():
            return Response(
                {"error": "not_configured",
                 "message": "PERPLEXITY_API_KEY is not set on the server."},
                status=400,
            )
        query = (request.data.get("query") or "").strip()
        if not query or len(query) > 500:
            return Response({"error": "query is required (max 500 chars)"}, status=400)

        active = SourceScan.objects.filter(
            website=website,
            status__in=[SourceScanStatus.PENDING, SourceScanStatus.RUNNING],
        ).count()
        if active >= self.MAX_ACTIVE_PER_WEBSITE:
            return Response(
                {"error": "too_many_active_scans"}, status=429,
            )

        scan = SourceScan.objects.create(
            website=website, query=query, created_by=request.user,
        )
        run_source_scan.delay(str(scan.id))
        return Response(_scan_summary(scan), status=201)


class SourceScanDetailView(TenantScopedAPIView):
    def get(self, request, website_id, scan_id):
        from apps.citations.models import SourceScan

        website = self.get_website(website_id)
        scan = self.get_tenant_object(
            SourceScan.objects.filter(website=website), pk=scan_id
        )
        from apps.citations.services.source_scan import (
            derive_opportunities,
            is_summary_derived,
        )

        payload = _scan_summary(scan)
        # Key is "rows", not "results": the EnvelopeRenderer treats any
        # dict containing "results" as DRF pagination output. Same for
        # "opportunities".
        payload["rows"] = [
            {
                "rank": r.rank,
                "url": r.url,
                "domain": r.domain,
                "source_class": r.source_class,
                "serp_title": r.serp_title,
                "serp_snippet": r.serp_snippet,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "last_updated_at": r.last_updated_at.isoformat() if r.last_updated_at else None,
                "relevant": r.relevant,
                "relevance_note": r.relevance_note,
                "fetch_status": r.fetch_status,
                "content_kind": r.content_kind,
                # True when the page could not be read and its brands were
                # taken from a Perplexity summary instead.
                "summary_derived": is_summary_derived(r),
                "word_count": r.word_count,
                "brands": r.brands,
                "analysis_error": r.analysis_error,
                # Which lane found this row, and its platform-native
                # metadata (subreddit, upvotes, comment count).
                "discovery": r.discovery,
                "platform_meta": r.platform_meta or {},
            }
            for r in scan.results.all()
        ]
        payload["opportunities"] = derive_opportunities(scan)
        # What each AI engine answered. Rows with status != "ok" are kept so
        # the UI can grey out an engine that has no key rather than omit it.
        payload["engines"] = [
            {
                "provider": a.provider,
                "model": a.model,
                "status": a.status,
                "answer_text": a.answer_text,
                "brands": a.brands,
                "citations": a.citations,
                "cited_ranks": a.cited_ranks,
                "error": a.error,
            }
            for a in scan.engine_answers.all()
        ]
        # People Also Ask, related searches, AI Overview, knowledge panel.
        payload["serp_features"] = scan.serp_features or {}
        return Response(payload)


class BrandLookupView(APIView):
    """Enrich a brand name with a canonical website + top web results.

    Backed by Perplexity search — the same client the scan pipeline
    uses, so we get the same daily quota + circuit breaker for free.
    Results are cached in Redis for 24h per (name, user) to keep
    repeat clicks off the quota. Silent-fail: an empty result payload
    means "we tried, nothing to show", not an error.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.citations.services import web_search

        name = (request.query_params.get("name") or "").strip()
        if not name or len(name) > 200:
            return Response({"error": "name is required (max 200 chars)"}, status=400)

        cache_key = f"brand_lookup:{request.user.pk}:{name.lower()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        results = web_search.search_web(
            name, max_results=6, user_id=request.user.pk,
        ) or []
        top = [
            {
                "rank": r.get("rank"),
                "url": r.get("url"),
                "domain": r.get("domain"),
                "title": r.get("title"),
                "snippet": (r.get("snippet") or "")[:280],
            }
            for r in results[:6]
        ]
        payload = {
            "name": name,
            "website": _guess_official_site(name, top),
            "top_results": top,
        }
        # 24h cache — brand SERP presence doesn't move that fast, and
        # clicking around a scan shouldn't burn the daily quota.
        cache.set(cache_key, payload, timeout=60 * 60 * 24)
        return Response(payload)


def _guess_official_site(name: str, results: list[dict]) -> str | None:
    """Heuristic: pick the first result whose domain contains a
    normalised slug of the brand name (letters + digits only, three
    or more chars). Falls back to the top result's origin if nothing
    obviously matches.
    """
    slug = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not slug or len(slug) < 3:
        return None
    for r in results:
        domain = (r.get("domain") or "").lower()
        if slug in re.sub(r"[^a-z0-9]", "", domain):
            url = r.get("url") or ""
            m = re.match(r"(https?://[^/]+)", url)
            return m.group(1) if m else None
    return None


def _scan_summary(scan) -> dict:
    return {
        "id": str(scan.id),
        "query": scan.query,
        "provider": scan.provider,
        "status": scan.status,
        "error": scan.error,
        "results_count": scan.results_count,
        "analyzed_count": scan.analyzed_count,
        # Per-lane progress. Older scans predate the field and get {}, which
        # the UI reads as "no lane breakdown available" and falls back to the
        # scan-level status.
        "stages": scan.stages or {},
        "brands": scan.brands,
        "own_brand_present": scan.own_brand_present,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
    }
