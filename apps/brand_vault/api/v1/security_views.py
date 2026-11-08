"""REST endpoints for Brand Security, under ``/api/v1/brand-security/``.

Findings come from the response auditor, which reads the AI answers the
audits and prompt runs already stored. There is no scan to trigger.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, time

from django.conf import settings
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brand_vault.api.v1.serializers import (
    BrandSecurityConfigSerializer,
    SafetyAlertSerializer,
)
from apps.brand_vault.models import BrandPulse, BrandSecurityConfig, SafetyAlert
from apps.brand_vault.services.security import response_auditor
from apps.brand_vault.services.security.detectors import (
    CATEGORIES,
    DETECTOR_INDEX,
    DETECTORS,
    ISSUE_FALLBACK,
)
from apps.rag.services.embedder import embeddings_configured
from core.exceptions import ResourceNotFound
from core.views import TenantScopedAPIView, TenantScopedListAPIView

_SEVERITY_WEIGHTS = {
    SafetyAlert.SEVERITY_HIGH: 10,
    SafetyAlert.SEVERITY_MEDIUM: 4,
    SafetyAlert.SEVERITY_LOW: 1,
}


_CATEGORY_DISPLAY = dict(CATEGORIES)


class BrandSecurityTaxonomyView(APIView):
    """The detector catalog: every trigger point the auditor can raise.

    Global (the registry is code, not tenant data) — hence a plain
    authenticated APIView rather than a tenant-scoped one. The frontend
    fetches this once and stops hardcoding detection vocabulary.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "engine": {
                "agent_id": response_auditor.AGENT_ID,
                "display_name": response_auditor.DISPLAY_NAME,
                "description": response_auditor.DESCRIPTION,
            },
            "categories": [
                {"key": key, "display": display} for key, display in CATEGORIES
            ],
            "detectors": [
                {
                    "code": d.code,
                    "slug": d.slug,
                    "display_name": d.display_name,
                    "category": d.category,
                    "category_display": _CATEGORY_DISPLAY.get(d.category, d.category),
                    "issue": d.issue,
                    "default_severity": d.default_severity,
                    "uses_llm_judge": d.judge_mode != "never",
                    "description": d.description,
                    "recommended_action": d.recommended_action,
                }
                for d in DETECTORS
            ],
        })


class BrandSecurityOverviewView(TenantScopedAPIView):
    """Top-of-page summary tiles: health, counters, and last-check timing.

    ``health_score`` is null and ``scan_status`` is "not_scanned" until
    the auditor has checked at least one stored answer for the website:
    no alerts on unchecked answers is not a clean bill of health.
    ``fact_checks`` is "unavailable" when the grounded fact check cannot
    run (no judge key, no embedding key, or the judge switched off);
    ``missing_keys`` names the keys to add.
    """

    def get(self, request, website_id):
        from apps.llm_ranking.models import LLMRankingAudit

        website = self.get_website(website_id)
        open_alerts = SafetyAlert.objects.filter(
            website=website, status=SafetyAlert.STATUS_OPEN,
        )

        by_severity = Counter(open_alerts.values_list("severity", flat=True))
        by_source = Counter(open_alerts.values_list("source", flat=True))

        # Per-detector and per-category counts for the summary chips.
        # Legacy rows without a detector code fall back via their issue.
        by_detector: Counter = Counter()
        by_category: Counter = Counter()
        for code, issue in open_alerts.values_list("detector_code", "issue"):
            code = code or ISSUE_FALLBACK.get(issue, "")
            detector = DETECTOR_INDEX.get(code)
            by_detector[code or "other"] += 1
            by_category[detector.category if detector else "other"] += 1

        # The auditor runs when an audit completes and again in the hourly
        # catch-up sweep, so the last check is the newest of: a completed
        # audit with stored answers, the sweep watermark, and a finding.
        config = BrandSecurityConfig.objects.filter(website=website).first()
        last_sweep_at = config.last_response_scan_at if config else None
        last_audit_at = (
            LLMRankingAudit.objects
            .filter(
                website=website,
                status=LLMRankingAudit.STATUS_COMPLETED,
                completed_at__isnull=False,
                results__query_succeeded=True,
            )
            .order_by("-completed_at")
            .values_list("completed_at", flat=True)
            .first()
        )
        latest_alert = (
            SafetyAlert.objects.filter(website=website)
            .order_by("-detected_at")
            .values_list("detected_at", flat=True)
            .first()
        )
        candidates = [t for t in (last_audit_at, last_sweep_at, latest_alert) if t]
        last_checked_at = max(candidates) if candidates else None

        if last_checked_at is None:
            health = None
            scan_status = "not_scanned"
        else:
            penalty = sum(
                _SEVERITY_WEIGHTS.get(sev, 0) * n
                for sev, n in by_severity.items()
            )
            health = max(0, min(100, 100 - penalty))
            scan_status = "scanned"

        missing_keys = []
        if not (getattr(settings, "ANTHROPIC_API_KEY", "") or ""):
            missing_keys.append("ANTHROPIC_API_KEY")
        if not embeddings_configured():
            missing_keys.append("OPENAI_API_KEY")
        judge_on = bool(getattr(settings, "BRAND_SECURITY_JUDGE_ENABLED", True)) and (
            config is None or config.llm_judge_enabled
        )

        return Response({
            "health_score": health,
            "scan_status": scan_status,
            "open_alerts": open_alerts.count(),
            "by_severity": {
                "high": by_severity.get(SafetyAlert.SEVERITY_HIGH, 0),
                "medium": by_severity.get(SafetyAlert.SEVERITY_MEDIUM, 0),
                "low": by_severity.get(SafetyAlert.SEVERITY_LOW, 0),
            },
            "by_source": dict(by_source),
            "by_detector": dict(by_detector),
            "by_category": dict(by_category),
            "last_checked_at": last_checked_at,
            "fact_checks": "available" if judge_on and not missing_keys else "unavailable",
            "missing_keys": missing_keys,
        })


def _parse_boundary(value: str | None, *, end: bool = False):
    """ISO datetime or bare date -> aware datetime (dates span the full day)."""
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        d = parse_date(value)
        if d is None:
            return None
        dt = datetime.combine(d, time.max if end else time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


class BrandSecurityAlertsView(TenantScopedListAPIView):
    """List alerts filterable by agent, severity, source, issue, detector,
    status, reference, free-text search and detection date range."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        # select_related: the serializer reads result.public_id and
        # result.source_prompt_id for the conversation/prompt links.
        qs = SafetyAlert.objects.filter(website=website).select_related("result")

        agent_ids = request.query_params.getlist("agent_id")
        if agent_ids:
            qs = qs.filter(agent_id__in=agent_ids)
        severities = request.query_params.getlist("severity")
        if severities:
            qs = qs.filter(severity__in=severities)
        sources = request.query_params.getlist("source")
        if sources:
            qs = qs.filter(source__in=sources)
        issues = request.query_params.getlist("issue")
        if issues:
            qs = qs.filter(issue__in=issues)
        detector_codes = request.query_params.getlist("detector_code")
        if detector_codes:
            qs = qs.filter(detector_code__in=detector_codes)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        # Exact reference lookup powers the UI's ?alert=BSA-... deep links.
        reference = request.query_params.get("reference")
        if reference:
            qs = qs.filter(reference__iexact=reference.strip())

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(reference__iexact=search)
                | Q(detector_code__iexact=search)
                | Q(title__icontains=search)
                | Q(snippet__icontains=search)
                | Q(detail__icontains=search)
            )

        detected_after = _parse_boundary(request.query_params.get("detected_after"))
        if detected_after:
            qs = qs.filter(detected_at__gte=detected_after)
        detected_before = _parse_boundary(
            request.query_params.get("detected_before"), end=True,
        )
        if detected_before:
            qs = qs.filter(detected_at__lte=detected_before)

        # Scope to findings raised from a single audit response, or from every
        # response for one library prompt. This is what lets the prompt detail
        # page show the findings against the answers it is already displaying,
        # instead of sending the reader to a global list to hunt for them.
        # The result param accepts the response's public_id (what the chat
        # modal navigates by) as well as the legacy integer PK.
        result_ref = (request.query_params.get("result") or "").strip()
        if result_ref:
            try:
                qs = qs.filter(result__public_id=uuid.UUID(result_ref))
            except ValueError:
                if result_ref.isdigit():
                    qs = qs.filter(result_id=int(result_ref))
                else:
                    qs = qs.none()
        source_prompt = request.query_params.get("source_prompt")
        if source_prompt:
            qs = qs.filter(result__source_prompt_id=source_prompt)

        ordering = request.query_params.get("ordering") or "-detected_at"
        if ordering == "severity":
            qs = qs.annotate(
                _sev_rank=Case(
                    When(severity=SafetyAlert.SEVERITY_HIGH, then=Value(0)),
                    When(severity=SafetyAlert.SEVERITY_MEDIUM, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                ),
            ).order_by("_sev_rank", "-detected_at")
        elif ordering in ("-last_seen_at", "detected_at", "-detected_at"):
            qs = qs.order_by(ordering)
        else:
            qs = qs.order_by("-detected_at")
        return self.paginated_response(qs, SafetyAlertSerializer)


class BrandSecurityAlertActionView(TenantScopedAPIView):
    """Resolve or dismiss an alert."""

    def post(self, request, alert_id, action):
        try:
            alert = SafetyAlert.objects.select_related("website").get(id=alert_id)
        except SafetyAlert.DoesNotExist as exc:
            raise ResourceNotFound("Alert not found.") from exc
        self.get_website(alert.website_id)

        if action == "dismiss":
            alert.status = SafetyAlert.STATUS_DISMISSED
        elif action == "resolve":
            alert.status = SafetyAlert.STATUS_RESOLVED
        else:
            return Response(
                {"error": "Unknown action."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        alert.resolved_at = timezone.now()
        alert.resolved_by = request.user
        alert.save(update_fields=["status", "resolved_at", "resolved_by", "updated_at"])
        return Response(SafetyAlertSerializer(alert).data)


class BrandSecurityConfigView(TenantScopedAPIView):
    """Read or update the per-website brand terms and the LLM judge switch."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        cfg, _ = BrandSecurityConfig.objects.get_or_create(website=website)
        return Response(BrandSecurityConfigSerializer(cfg).data)

    def put(self, request, website_id):
        website = self.get_website(website_id)
        cfg, _ = BrandSecurityConfig.objects.get_or_create(website=website)
        terms = request.data.get("brand_terms")
        if isinstance(terms, list):
            cfg.brand_terms = [str(x).strip() for x in terms if str(x).strip()]
        llm_judge = request.data.get("llm_judge_enabled")
        if isinstance(llm_judge, bool):
            cfg.llm_judge_enabled = llm_judge
        cfg.save()
        return Response(BrandSecurityConfigSerializer(cfg).data)


class BrandPulseView(TenantScopedAPIView):
    """Read or update the per-website Brand Pulse agent settings."""

    @staticmethod
    def _payload(pulse: BrandPulse) -> dict:
        return {
            "enabled": pulse.enabled,
            "auto_scan": pulse.auto_scan,
            "frequency": pulse.frequency,
            "last_digest_at": pulse.last_digest_at,
            "last_scan_queued_at": pulse.last_scan_queued_at,
        }

    def get(self, request, website_id):
        website = self.get_website(website_id)
        pulse, _ = BrandPulse.objects.get_or_create(website=website)
        return Response(self._payload(pulse))

    def put(self, request, website_id):
        website = self.get_website(website_id)
        pulse, _ = BrandPulse.objects.get_or_create(website=website)
        enabled = request.data.get("enabled")
        if isinstance(enabled, bool):
            pulse.enabled = enabled
        auto_scan = request.data.get("auto_scan")
        if isinstance(auto_scan, bool):
            pulse.auto_scan = auto_scan
        frequency = request.data.get("frequency")
        if frequency in (BrandPulse.FREQUENCY_DAILY, BrandPulse.FREQUENCY_WEEKLY):
            pulse.frequency = frequency
        pulse.save()
        return Response(self._payload(pulse))
