"""REST endpoints for Brand Security.

Live under ``/api/v1/brand-security/``. The legacy ``/brand-vault/safety/*``
endpoints in ``views.py`` stay in place for a release so existing UI clients
keep working while we migrate. Prefer these views for new work.
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, time

from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brand_vault.api.v1.serializers import (
    BrandSecurityAgentPatchSerializer,
    BrandSecurityAgentSerializer,
    BrandSecurityConfigSerializer,
    SafetyAlertSerializer,
    SafetyPromptCreateSerializer,
    SafetyPromptSerializer,
)
from apps.brand_vault.models import (
    BrandPulse,
    BrandSecurityAgent,
    BrandSecurityConfig,
    SafetyAlert,
    SafetyPrompt,
)
from apps.brand_vault.services.security import response_auditor
from apps.brand_vault.services.security.detectors import (
    CATEGORIES,
    DETECTOR_INDEX,
    DETECTORS,
    ISSUE_FALLBACK,
)
from apps.brand_vault.services.security.orchestrator import ensure_agent_rows
from apps.brand_vault.services.security.registry import agent_catalog
from core.exceptions import ResourceNotFound
from core.views import TenantScopedAPIView, TenantScopedListAPIView

_SEVERITY_WEIGHTS = {
    SafetyAlert.SEVERITY_HIGH: 10,
    SafetyAlert.SEVERITY_MEDIUM: 4,
    SafetyAlert.SEVERITY_LOW: 1,
}


# Starter prompts seeded on first activation. {brand} is substituted with the
# first brand term (falls back to website name/domain).
_STARTER_PROMPTS: dict[str, tuple[str, ...]] = {
    "llm_truth": (
        "What is {brand}?",
        "Is {brand} a scam?",
        "Tell me about {brand}",
        "Is {brand} legit?",
    ),
    "serp_reputation": (
        "{brand} reviews",
        "{brand} complaints",
        "{brand} scam",
        "{brand} lawsuit",
    ),
    "sentiment_pulse": (
        "{brand}",
        "bad experience with {brand}",
        "{brand} review",
        "is {brand} legit",
    ),
}


def _resolve_brand_name(website) -> str:
    """Best-effort brand name for prompt seeding. Falls back to website name."""
    from apps.brand_vault.services.security.agents._helpers import brand_terms

    terms = brand_terms(website, {})
    if terms:
        return terms[0]
    return (website.name or "").strip() or "our brand"


def _seed_starter_prompts(website, agent_id: str, user) -> int:
    """Insert starter prompts for ``agent_id`` if the list is empty. Returns
    the number of prompts inserted (0 if the agent has no starter set or
    already had prompts).
    """
    templates = _STARTER_PROMPTS.get(agent_id)
    if not templates:
        return 0
    if SafetyPrompt.objects.filter(website=website, agent_id=agent_id).exists():
        return 0
    brand = _resolve_brand_name(website)
    inserted = 0
    for tpl in templates:
        text = tpl.format(brand=brand)[:500]
        _, created = SafetyPrompt.objects.get_or_create(
            website=website, agent_id=agent_id, text=text,
            defaults={"created_by": user if user and user.is_authenticated else None},
        )
        if created:
            inserted += 1
    return inserted


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
    """Top-of-page summary tiles: health, counters, and last-scan timing."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        open_alerts = SafetyAlert.objects.filter(
            website=website, status=SafetyAlert.STATUS_OPEN,
        )

        by_severity = Counter(open_alerts.values_list("severity", flat=True))
        penalty = sum(
            _SEVERITY_WEIGHTS.get(sev, 0) * n
            for sev, n in by_severity.items()
        )
        health = max(0, min(100, 100 - penalty))

        by_source = Counter(open_alerts.values_list("source", flat=True))
        by_agent = Counter(open_alerts.values_list("agent_id", flat=True))

        # Per-detector and per-category counts for the summary chips.
        # Legacy rows without a detector code fall back via their issue.
        by_detector: Counter = Counter()
        by_category: Counter = Counter()
        for code, issue in open_alerts.values_list("detector_code", "issue"):
            code = code or ISSUE_FALLBACK.get(issue, "")
            detector = DETECTOR_INDEX.get(code)
            by_detector[code or "other"] += 1
            by_category[detector.category if detector else "other"] += 1

        agents = ensure_agent_rows(website)
        last_run_at = max(
            (a.last_run_at for a in agents if a.last_run_at),
            default=None,
        )
        next_run_at = min(
            (a.next_run_at for a in agents if a.next_run_at),
            default=None,
        )

        # The auditor runs on completion hooks, so "last checked" is the
        # newest scanned response rather than an agent run timestamp.
        config = BrandSecurityConfig.objects.filter(website=website).first()
        last_scan_at = config.last_response_scan_at if config else None
        latest_alert = (
            SafetyAlert.objects.filter(website=website)
            .order_by("-detected_at")
            .values_list("detected_at", flat=True)
            .first()
        )
        candidates = [t for t in (last_run_at, last_scan_at, latest_alert) if t]
        last_checked_at = max(candidates) if candidates else None

        return Response({
            "health_score": health,
            "open_alerts": open_alerts.count(),
            "by_severity": {
                "high": by_severity.get(SafetyAlert.SEVERITY_HIGH, 0),
                "medium": by_severity.get(SafetyAlert.SEVERITY_MEDIUM, 0),
                "low": by_severity.get(SafetyAlert.SEVERITY_LOW, 0),
            },
            "by_source": dict(by_source),
            "by_agent": dict(by_agent),
            "by_detector": dict(by_detector),
            "by_category": dict(by_category),
            "last_run_at": last_run_at,
            "last_checked_at": last_checked_at,
            "next_run_at": next_run_at,
            "prompts_monitored": SafetyPrompt.objects.filter(
                website=website, status=SafetyPrompt.STATUS_ACTIVE,
            ).count(),
        })


class BrandSecurityAgentsView(TenantScopedAPIView):
    """List every registered agent with its per-website state."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        ensure_agent_rows(website)

        catalog = {row["agent_id"]: row for row in agent_catalog()}
        rows = list(
            BrandSecurityAgent.objects.filter(website=website).order_by("agent_id"),
        )

        # One aggregation query for open-alert counts by (agent, severity).
        counts = (
            SafetyAlert.objects.filter(
                website=website, status=SafetyAlert.STATUS_OPEN,
            )
            .values("agent_id", "severity")
            .annotate(n=Count("id"))
        )
        agg: dict[str, dict[str, int]] = {}
        for c in counts:
            agg.setdefault(c["agent_id"], {})[c["severity"]] = c["n"]

        payload = []
        for row in rows:
            meta = catalog.get(row.agent_id, {})
            by_sev = agg.get(row.agent_id, {})
            row.display_name = meta.get("display_name", row.agent_id)
            row.tagline = meta.get("tagline", "")
            row.color = meta.get("color", "slate")
            row.sources = meta.get("sources", [])
            row.open_high = by_sev.get(SafetyAlert.SEVERITY_HIGH, 0)
            row.open_medium = by_sev.get(SafetyAlert.SEVERITY_MEDIUM, 0)
            row.open_low = by_sev.get(SafetyAlert.SEVERITY_LOW, 0)
            row.open_alerts = row.open_high + row.open_medium + row.open_low
            payload.append(BrandSecurityAgentSerializer(row).data)

        return Response(payload)


class BrandSecurityAgentDetailView(TenantScopedAPIView):
    """PATCH an agent's config (enable, sensitivity, schedule)."""

    def _get(self, request, website_id, agent_id) -> BrandSecurityAgent:
        website = self.get_website(website_id)
        try:
            row = BrandSecurityAgent.objects.get(
                website=website, agent_id=agent_id,
            )
        except BrandSecurityAgent.DoesNotExist as exc:
            raise ResourceNotFound("Agent not found.") from exc
        return row

    def patch(self, request, website_id, agent_id):
        row = self._get(request, website_id, agent_id)
        ser = BrandSecurityAgentPatchSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        was_enabled = row.enabled
        for field, value in ser.validated_data.items():
            setattr(row, field, value)
        row.save()
        # Seed starter prompts the first time a prompt-driven agent is
        # activated so the user has a working list to edit.
        if (
            not was_enabled and row.enabled
            and agent_id in _STARTER_PROMPTS
        ):
            _seed_starter_prompts(row.website, agent_id, request.user)
        return Response(BrandSecurityAgentSerializer(row).data)


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
    """Read or update the per-website brand terms + negative keywords."""

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
        negatives = request.data.get("negative_keywords")
        if isinstance(negatives, list):
            cfg.negative_keywords = [
                str(x).strip() for x in negatives if str(x).strip()
            ]
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


class BrandSecurityPromptsView(TenantScopedListAPIView):
    """List or create SafetyPrompts. Filter by ``?agent_id=xxx`` to scope."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = SafetyPrompt.objects.filter(website=website)
        agent_id = request.query_params.get("agent_id")
        if agent_id:
            qs = qs.filter(agent_id=agent_id)
        qs = qs.order_by("-created_at")
        return self.paginated_response(qs, SafetyPromptSerializer)

    def post(self, request, website_id):
        website = self.get_website(website_id)
        ser = SafetyPromptCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["text"].strip()
        agent_id = ser.validated_data.get("agent_id") or "llm_truth"
        prompt, created = SafetyPrompt.objects.get_or_create(
            website=website, agent_id=agent_id, text=text,
            defaults={"created_by": request.user},
        )
        return Response(
            SafetyPromptSerializer(prompt).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class BrandSecurityPromptDetailView(TenantScopedAPIView):
    """Delete a SafetyPrompt."""

    def delete(self, request, prompt_id):
        try:
            prompt = SafetyPrompt.objects.select_related("website").get(id=prompt_id)
        except SafetyPrompt.DoesNotExist as exc:
            raise ResourceNotFound("Prompt not found.") from exc
        self.get_website(prompt.website_id)
        prompt.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Security perception ─────────────────────────────────────────────────


class BrandSecurityPerceptionView(TenantScopedAPIView):
    """What AI assistants say about the brand's security posture.

    ``?days=`` sets the window (default 90, max 365, 0 = all time). Every
    number is perception: what the models said, checked against the
    brand's own uploaded material, never a statement about the product's
    actual security.
    """

    def get(self, request, website_id):
        from apps.brand_vault.services.security.perception import (
            DEFAULT_WINDOW_DAYS,
            build_security_perception,
        )

        website = self.get_website(website_id)
        try:
            days = int(request.query_params.get("days", DEFAULT_WINDOW_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_WINDOW_DAYS
        days = max(0, min(365, days))
        return Response(build_security_perception(website, days=days))


class BrandSecurityProbeView(TenantScopedAPIView):
    """Start a security-perception probe: a cold audit over the security
    prompt pack (or the saved prompts tagged "security")."""

    MAX_CUSTOM_PROMPTS = 10

    def post(self, request, website_id):
        from apps.llm_ranking.providers import PROVIDERS
        from apps.llm_ranking.services.security_probe import start_security_probe
        from core.ai_tracking import effective_ai_cap, month_to_date_cost

        website = self.get_website(website_id)

        cap = effective_ai_cap(request.user)
        if cap > 0:
            spent = month_to_date_cost(request.user)
            if spent >= cap:
                return Response(
                    {
                        "error": "monthly_ai_cost_cap_exceeded",
                        "detail": (
                            f"Month-to-date AI spend ${spent:.2f} has reached "
                            f"this account's monthly allowance of ${cap:.2f}."
                        ),
                        "cap_status": {"spent_usd": round(spent, 4), "cap_usd": cap},
                    },
                    status=402,
                )

        raw_providers = request.data.get("providers") or []
        if not isinstance(raw_providers, list):
            return Response({"error": "providers must be a list."}, status=400)
        providers = [p for p in raw_providers if isinstance(p, str) and p in PROVIDERS]

        raw_prompts = request.data.get("custom_prompts") or []
        if not isinstance(raw_prompts, list):
            return Response({"error": "custom_prompts must be a list."}, status=400)
        custom_prompts = [
            str(p).strip()[:500] for p in raw_prompts if isinstance(p, str) and p.strip()
        ][: self.MAX_CUSTOM_PROMPTS]

        try:
            audit = start_security_probe(
                website, request.user,
                providers=providers or None,
                custom_prompts=custom_prompts or None,
            )
        except ValueError as exc:
            return Response(
                {"error": str(exc), "code": "no_security_prompts"}, status=400,
            )
        return Response(
            {
                "audit_id": str(audit.id),
                "status": audit.status,
                "probe_kind": audit.probe_kind,
                "prompt_count": len(audit.prompts or []),
                "providers": audit.providers_queried,
            },
            status=status.HTTP_202_ACCEPTED,
        )
