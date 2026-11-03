"""REST endpoints for the prompt library.

The API has two audiences:

* The onboarding flow + LLM Ranking dashboard, which need read-only
  access to industries and a paginated browse of prompts.
* The Run Audit modal, which previews and persists a per-audit
  prompt sample (a :class:`PromptSampleRun`).
"""
from __future__ import annotations

import logging
import random

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.llm_ranking.models import LLMRankingAudit
from apps.prompt_library.api.v1.serializers import (
    AutoTemplateRequestSerializer,
    BrandPromptSerializer,
    CreatePromptScheduleSerializer,
    GenerateFromContextRequestSerializer,
    GenerateRelatedRequestSerializer,
    IndustrySerializer,
    PreviewSampleRequestSerializer,
    PromptCreateSerializer,
    PromptSampleRunSerializer,
    PromptScheduleSerializer,
    PromptSerializer,
    PromptVariableSetSerializer,
    SmokeTestRequestSerializer,
    SynthesizeRequestSerializer,
    UseLibrarySampleRequestSerializer,
)
from apps.prompt_library.models import (
    BrandPrompt,
    Industry,
    Prompt,
    PromptSchedule,
    PromptSource,
    PromptVariableSet,
    RejectedBrandPrompt,
)
from apps.prompt_library.services.sampler_service import sample_prompts_for_audit
from apps.websites.services.website_service import WebsiteService
from core.interceptors.pagination import StandardPagination
from core.views import TenantScopedAPIView

logger = logging.getLogger("apps")


def _public_model_error(raw: str) -> str:
    """Collapse a raw provider error into a safe, generic message.

    Raw errors leak configuration details -- key validity, console URLs,
    account hints -- which look alarming to users and double as
    reconnaissance for attackers probing the deployment. The full text
    stays in the DB rows and server logs for operators; API clients only
    ever learn the category.
    """
    e = (raw or "").lower()
    if not e:
        return ""
    if "rate limit" in e or "429" in e or "quota" in e or "overloaded" in e:
        return "The model was busy and will be retried on the next scan."
    if "timeout" in e or "timed out" in e:
        return "The model did not respond in time."
    return "This model did not respond on this scan."


def _public_scan_error(raw: str) -> str:
    """Run-level variant of :func:`_public_model_error`."""
    return (
        "The scan could not complete. The issue has been logged."
        if (raw or "").strip() else ""
    )


def _match_brand_domain(brand_name: str, domains: list[str]) -> str | None:
    """Best-effort match of a brand name to one of the cited domains.

    Squashes both to alphanumerics and matches when the domain's root
    contains the brand (or vice-versa), or they share a distinctive token.
    Returns the matched apex domain or None. Used to source a real website
    logo from the domains the models actually cited.
    """
    import re

    squashed = re.sub(r"[^a-z0-9]", "", (brand_name or "").lower())
    if len(squashed) < 3 or not domains:
        return None
    tokens = {t for t in re.split(r"[^a-z0-9]+", (brand_name or "").lower()) if len(t) >= 4}
    best = None
    for d in domains:
        root = (d or "").split(".")[0].lower()
        root_sq = re.sub(r"[^a-z0-9]", "", root)
        if not root_sq:
            continue
        if root_sq == squashed or root_sq in squashed or squashed in root_sq:
            return d  # strong match — take it immediately
        if best is None and any(tok in root_sq for tok in tokens):
            best = d  # weaker token match — keep as fallback
    return best


class IndustryListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = IndustrySerializer
    pagination_class = None

    def get_queryset(self):
        return Industry.objects.filter(is_active=True).order_by("name")


class PromptListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PromptSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = Prompt.objects.filter(is_active=True).select_related("industry")
        industry_slug = self.request.query_params.get("industry")
        if industry_slug:
            qs = qs.filter(industry__slug=industry_slug)
        intent = self.request.query_params.get("intent_bucket")
        if intent:
            qs = qs.filter(intent_bucket=intent)
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(text__icontains=search)
        return qs.order_by("-demand_score", "-created_at")


class PreviewSampleView(APIView):
    """Return a non-persisted sample for the Run Audit preview drawer."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PreviewSampleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        industry = get_object_or_404(Industry, id=data["industry_id"], is_active=True)
        n = data["n"]
        seed = random.randint(0, 2**31 - 1)
        rng = random.Random(seed)

        pool = list(
            Prompt.objects.filter(industry=industry, is_active=True).order_by(
                "-demand_score", "-created_at"
            )[: max(n * 4, 50)]
        )
        rng.shuffle(pool)
        chosen = pool[:n]
        return Response(
            {
                "industry": IndustrySerializer(industry).data,
                "seed": seed,
                "strategy": data["strategy"],
                "prompts": PromptSerializer(chosen, many=True).data,
            }
        )


def _resolve_audit(user, audit_id) -> LLMRankingAudit:
    """Fetch an audit and verify the user owns the underlying website."""
    audit = get_object_or_404(LLMRankingAudit, id=audit_id)
    WebsiteService.get_for_user(user=user, website_id=audit.website_id)
    return audit


class UseLibrarySampleView(APIView):
    """Persist a PromptSampleRun on a draft audit and flip prompt_source."""

    permission_classes = [IsAuthenticated]

    def post(self, request, audit_id):
        audit = _resolve_audit(request.user, audit_id)
        serializer = UseLibrarySampleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        industry = get_object_or_404(Industry, id=data["industry_id"], is_active=True)
        if audit.status not in (LLMRankingAudit.STATUS_PENDING,):
            return Response(
                {"error": "audit_not_draft", "detail": "Sample can only be set on a pending audit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sample_run = sample_prompts_for_audit(
            audit_run=audit,
            industry=industry,
            n=data["n"],
            strategy=data["strategy"],
            seed=data.get("seed"),
        )
        # Default to library when the user explicitly opts in.
        if audit.prompt_source == LLMRankingAudit.PROMPT_SOURCE_VAULT:
            audit.prompt_source = LLMRankingAudit.PROMPT_SOURCE_LIBRARY
            audit.save(update_fields=["prompt_source"])
        return Response(PromptSampleRunSerializer(sample_run).data)


class GetAuditSampleView(APIView):
    """Return the persisted sample with provenance for the audit detail page."""

    permission_classes = [IsAuthenticated]

    def get(self, request, audit_id):
        audit = _resolve_audit(request.user, audit_id)
        sample_run = getattr(audit, "prompt_sample_run", None)
        if sample_run is None:
            return Response({"sample_run": None})
        return Response({"sample_run": PromptSampleRunSerializer(sample_run).data})


class IndustryTrendView(APIView):
    """Return cached Google Trends data for an industry."""

    permission_classes = [IsAuthenticated]

    def get(self, request, slug):
        from apps.prompt_library.services.trends_service import get_trend_payload

        industry = get_object_or_404(Industry, slug=slug, is_active=True)
        return Response(get_trend_payload(industry))


class WebsiteBrandPromptsView(TenantScopedAPIView):
    """List or add brand prompts for a website."""

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = (
            BrandPrompt.objects.filter(website=website)
            .select_related("prompt", "prompt__industry")
            .order_by("-created_at")
        )
        return Response(BrandPromptSerializer(qs, many=True).data)

    def post(self, request, website_id):
        website = self.get_website(website_id)
        serializer = BrandPromptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        prompt = get_object_or_404(
            Prompt, id=serializer.validated_data["prompt_id"], is_active=True
        )
        bp, created = BrandPrompt.objects.get_or_create(
            website=website,
            prompt=prompt,
            defaults={"notes": serializer.validated_data.get("notes", "")},
        )
        return Response(
            BrandPromptSerializer(bp).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class BrandPromptDetailView(APIView):
    """Update or delete a single brand-prompt entry.

    Both actions are tenant-scoped: the row is fetched by id, then
    WebsiteService.get_for_user 404s unless the caller owns the website
    it belongs to — so one tenant can never touch another's saved
    prompts.
    """

    permission_classes = [IsAuthenticated]

    def _owned(self, request, brand_prompt_id):
        bp = get_object_or_404(BrandPrompt, id=brand_prompt_id)
        WebsiteService.get_for_user(user=request.user, website_id=bp.website_id)
        return bp

    def patch(self, request, brand_prompt_id):
        bp = self._owned(request, brand_prompt_id)
        if "is_archived" in request.data:
            bp.is_archived = bool(request.data["is_archived"])
            bp.save(update_fields=["is_archived", "updated_at"])
        return Response(BrandPromptSerializer(bp).data)

    def delete(self, request, brand_prompt_id):
        bp = self._owned(request, brand_prompt_id)
        bp.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Phase 3: templated prompts, effectiveness, variables, smoke test ──

class RegionsListView(APIView):
    """GET — country options for the Add Prompt location picker."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.llm_ranking.services.regions import supported_countries
        return Response({"countries": supported_countries()})


class WebsiteSamplePromptsView(TenantScopedAPIView):
    """Generate a starter set of diverse saved prompts for a website.

    POST /api/v1/prompt-library/websites/<website_id>/prompts/generate-samples/
    body: {"count": 1-15, default 10}

    Reads the site first (when the website has no description), then asks
    Claude for prompts written for what the business actually does.
    Prompts are neutral by design (no brand name in the text) so the first
    scan measures real visibility rather than parroting the brand back.
    Re-running is safe: the registrar dedupes on text hash.

    409 {"error": "synthesis_not_configured"} without a Claude key;
    502 {"error": "synthesis_failed"} when the call fails. Nothing is
    substituted in either case.
    """

    def post(self, request, website_id):
        from apps.llm_ranking.services.prompt_registrar import (
            register_generated_prompts,
        )
        from apps.prompt_library.services.sample_prompts import build_sample_prompts
        from core.exceptions import RateLimited
        from core.resilience import TokenBucket

        website = self.get_website(website_id)
        bucket = TokenBucket(
            name=f"sample-prompts:{request.user.id}", capacity=4,
            refill_per_second=0.05,
        )
        if not bucket.try_acquire():
            raise RateLimited()

        try:
            count = int(request.data.get("count", 10))
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(count, 15))

        from apps.prompt_library.services.context_generator import (
            SynthesisFailed,
            SynthesisNotConfigured,
        )

        try:
            result = build_sample_prompts(website, count=count, user=request.user)
        except SynthesisNotConfigured:
            return Response(
                {"error": "synthesis_not_configured"},
                status=status.HTTP_409_CONFLICT,
            )
        except SynthesisFailed:
            return Response(
                {"error": "synthesis_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        items = result["items"]

        # We could not learn enough about the site to build anything
        # relevant. Ask for a description rather than emit a generic list.
        if result["source"] == "none" or not items:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "site_not_classified",
                        "message": (
                            "We couldn't read enough from this site to build "
                            "relevant prompts. Add a short description of what "
                            "the business does, then try again."
                        ),
                    },
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        created = register_generated_prompts(items, website)

        return Response(
            {
                "generated": [
                    {"text": i["text"], "intent": i.get("type", "custom")}
                    for i in items
                ],
                "created_count": created,
                "total": len(items),
                "source": result["source"],
            },
            status=status.HTTP_201_CREATED,
        )


class WebsitePromptCreateView(TenantScopedAPIView):
    """Create a manual templated prompt for a website."""

    def post(self, request, website_id):
        from apps.prompt_library.services._hash import text_hash
        from apps.prompt_library.services.template_parser import extract_variables

        website = self.get_website(website_id)
        serializer = PromptCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        industry = None
        topic_name = (data.get("topic") or "").strip()
        if topic_name:
            # Topic == bundle. Resolve or create the matching Industry so the
            # prompt (and any citations it later produces) file under it.
            from django.utils.text import slugify
            slug = slugify(topic_name)[:64] or "topic"
            industry, _ = Industry.objects.get_or_create(
                slug=slug,
                defaults={"name": topic_name, "is_active": True},
            )
        elif data.get("industry_id"):
            industry = get_object_or_404(Industry, id=data["industry_id"], is_active=True)
        else:
            # The website's own industry when it matches one; otherwise the
            # inactive "Uncategorized" bucket, never an unrelated industry.
            from apps.prompt_library.services.industry_match import (
                industry_for_website,
            )
            industry = industry_for_website(website)

        tags = [t.strip() for t in (data.get("tags") or []) if t and t.strip()]
        location = (data.get("location") or "").strip()
        model_variants = data.get("models") or []

        # Each non-empty line becomes a separate prompt (matches the modal's
        # "every line is a prompt" + bulk-upload behaviour). Template-only
        # payloads (no newlines) collapse to a single prompt as before.
        raw = (data.get("text") or "").strip()
        template_text_in = (data.get("template_text") or "").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            lines = [template_text_in] if template_text_in else []

        created_prompts = []
        brand_prompt_ids = []
        for line in lines:
            template_text = template_text_in or line
            body_for_text = line or template_text
            variables = extract_variables(template_text)
            canonical_hash = text_hash(template_text or body_for_text)
            prompt, _ = Prompt.objects.get_or_create(
                text_hash=canonical_hash,
                defaults=dict(
                    industry=industry,
                    text=body_for_text,
                    template_text=template_text,
                    template_variables=variables,
                    intent_bucket=data.get("intent_bucket") or "category",
                    style=data.get("style") or "question",
                    source=PromptSource.MANUAL,
                ),
            )
            brand_prompt, _ = BrandPrompt.objects.get_or_create(
                website=website, prompt=prompt,
            )
            changed = []
            if tags and brand_prompt.tags != tags:
                brand_prompt.tags = tags
                changed.append("tags")
            if location and brand_prompt.location != location:
                brand_prompt.location = location
                changed.append("location")
            if model_variants and brand_prompt.model_variants != model_variants:
                brand_prompt.model_variants = model_variants
                changed.append("model_variants")
            if changed:
                brand_prompt.save(update_fields=[*changed, "updated_at"])
            created_prompts.append(prompt)
            brand_prompt_ids.append(str(brand_prompt.id))

        if not created_prompts:
            return Response({"error": "no_prompts"}, status=status.HTTP_400_BAD_REQUEST)

        # Auto-scan: run the new prompts through the ranking pipeline so they
        # start collecting mentions/citations. Never fails the create: the
        # prompts are saved either way, and ``scan_error`` says why no scan
        # started. A custom model selection routes through the per-prompt
        # crawler, the only path that honors BrandPrompt.model_variants.
        scan_audit_id, scan_error = self._trigger_scan(
            website, request.user, [p.text for p in created_prompts], location,
            crawl_prompt_ids=(
                [str(p.id) for p in created_prompts] if model_variants else None
            ),
        )

        payload = PromptSerializer(created_prompts[0]).data
        payload["brand_prompt_id"] = brand_prompt_ids[0]
        payload["brand_prompt_ids"] = brand_prompt_ids
        payload["created_count"] = len(created_prompts)
        payload["scan_audit_id"] = scan_audit_id
        payload["scan_error"] = scan_error
        return Response(payload, status=status.HTTP_201_CREATED)

    @staticmethod
    def _trigger_scan(website, user, prompts, location, crawl_prompt_ids=None):
        """Kick off a scan for freshly added prompts.

        Returns ``(audit_id, error)``. ``error`` is None when a scan was
        queued, the 409 body ``{"error": "no_llm_provider_configured",
        "missing": [...]}`` when no AI engine has a key, ``{"error": code,
        "message": ...}`` for a refusal such as the prompt allowance, and
        ``{"error": "scan_not_started"}`` when queueing failed.

        Default: one LLM ranking audit over all created prompts. When
        ``crawl_prompt_ids`` is set (custom model selection), each prompt
        is dispatched through the per-prompt crawler instead — the audit
        pipeline always queries provider-default models and would ignore
        the selection.
        """
        from apps.llm_ranking.services.audit_factory import (
            NoLLMProviderConfigured,
            configured_providers,
            create_audit,
            missing_provider_keys,
        )
        from core.exceptions import CanseeException

        if not configured_providers():
            return None, NoLLMProviderConfigured(missing_provider_keys()).response_body()
        try:
            if crawl_prompt_ids:
                from apps.prompt_library.tasks import crawl_prompt_for_website
                for prompt_id in crawl_prompt_ids:
                    crawl_prompt_for_website.delay(
                        str(website.id), str(prompt_id),
                        acting_user_id=str(user.id) if user else None,
                    )
                return None, None
            from apps.llm_ranking.services.scan_dispatch import dispatch_scan

            audit = create_audit(
                website=website,
                user=user,
                prompts=list(prompts),
                location=location or "",
            )
            dispatch_scan(str(audit.id))
            return str(audit.id), None
        except NoLLMProviderConfigured as exc:
            return None, exc.response_body()
        except CanseeException as exc:
            # e.g. the monthly prompt allowance is used up.
            return None, {"error": exc.code, "message": exc.message}
        except Exception:  # pragma: no cover - the create must not fail
            logger.exception("auto-scan dispatch failed")
            return None, {"error": "scan_not_started"}


class PromptPreviewView(APIView):
    """Return a filled template + missing variables for a (prompt, website)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, prompt_id):
        from apps.prompt_library.services.variable_resolver import resolve_for_website

        prompt = get_object_or_404(Prompt, id=prompt_id)
        website_id = request.query_params.get("website")
        website = None
        if website_id:
            website = WebsiteService.get_for_user(user=request.user, website_id=website_id)
        filled, missing = resolve_for_website(website, prompt)
        return Response({
            "prompt_id": str(prompt.id),
            "filled_text": filled,
            "missing_variables": missing,
            "template_variables": list(prompt.template_variables or []),
        })


class PromptEffectivenessView(APIView):
    """Return the cached effectiveness envelope for a prompt."""

    permission_classes = [IsAuthenticated]

    def get(self, request, prompt_id):
        prompt = get_object_or_404(Prompt, id=prompt_id)
        components = dict(prompt.effectiveness_components or {})
        stable = bool(components.pop("stable", prompt.runs_count >= 3))
        runs = int(components.pop("runs", prompt.runs_count or 0))
        return Response({
            "prompt_id": str(prompt.id),
            "overall": prompt.effectiveness_score,
            "components": components,
            "stable": stable,
            "runs": runs,
        })


class PromptAutoTemplateView(APIView):
    """Convert raw text to a {{ variable }} template via the synthesis provider."""

    permission_classes = [IsAuthenticated]
    _BUCKET_CAPACITY = 10
    _BUCKET_REFILL = 10 / 60.0  # 10 calls per minute, refilled smoothly.

    def post(self, request):
        from apps.prompt_library.services.auto_template import auto_template
        from core.resilience import TokenBucket

        # Per-user rate guard. This endpoint makes a billable synthesis call,
        # so it must be both attributed (user=request.user, so the provider
        # spend cap applies) and throttled (so one user cannot spin the
        # synthesis provider in a loop).
        bucket = TokenBucket(
            name=f"prompt_auto_template:{request.user.id}",
            capacity=self._BUCKET_CAPACITY,
            refill_per_second=self._BUCKET_REFILL,
        )
        if not bucket.try_acquire():
            return Response(
                {"error": "rate_limited", "detail": "Try again in a few seconds."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = AutoTemplateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        envelope = auto_template(
            serializer.validated_data["raw_text"], user=request.user,
        )
        return Response(envelope)


class PromptSearchSourcesView(APIView):
    """Live Google search lookup so the UI can show 'where this question is
    being asked from' (top organic results, classified by source domain).

    Body: ``{"query": "the prompt text"}``
    Returns: ``{"provider": "serpapi" | "unconfigured", "query": str,
                "results": [{title, url, domain, snippet, source_class, position}]}``
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.prompt_library.services.search_sources import search_sources

        query = (request.data or {}).get("query") or ""
        query = str(query).strip()
        if len(query) < 5:
            return Response(
                {"detail": "Query too short."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(search_sources(query, limit=8))


class PromptSmokeTestView(APIView):
    """Run a single prompt against one provider and return quality signals.

    Rate-limited per user via the existing token bucket (the underlying
    provider already enforces a per-provider RPM cap, so this view adds
    a coarser per-user guard to keep one user from saturating the bucket).
    """

    permission_classes = [IsAuthenticated]
    _SMOKE_BUCKET_CAPACITY = 10
    _SMOKE_BUCKET_REFILL = 10 / 60.0  # 10 calls per minute, refilled smoothly.

    def post(self, request, prompt_id):
        from apps.prompt_library.services.smoke_test import smoke_test_prompt
        from core.resilience import TokenBucket

        bucket = TokenBucket(
            name=f"prompt_smoke:{request.user.id}",
            capacity=self._SMOKE_BUCKET_CAPACITY,
            refill_per_second=self._SMOKE_BUCKET_REFILL,
        )
        if not bucket.try_acquire():
            return Response(
                {"error": "rate_limited", "detail": "Try again in a few seconds."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        prompt = get_object_or_404(Prompt, id=prompt_id)
        serializer = SmokeTestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        website = WebsiteService.get_for_user(
            user=request.user, website_id=data["website_id"],
        )
        envelope = smoke_test_prompt(
            prompt, website, provider=data["provider"], user=request.user,
        )
        return Response(envelope)


class PromptSynthesizeView(APIView):
    """Bulk-generate prompt templates via the synthesis provider."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.llm_ranking.providers import get_synthesis_provider
        from apps.prompt_library.services._hash import text_hash
        from apps.prompt_library.services.auto_template import auto_template

        serializer = SynthesizeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        industry = get_object_or_404(Industry, id=data["industry_id"], is_active=True)

        provider = get_synthesis_provider()
        if provider is None:
            return Response(
                {"error": "synthesis_not_configured"},
                status=status.HTTP_409_CONFLICT,
            )

        body = (
            f"Generate {data['count']} distinct {data['style']}-style prompts a real "
            f"user might ask an AI assistant in the '{industry.name}' industry. Use "
            f"snake_case {{{{ variables }}}} for any tenant-specific noun (brand, "
            f"location, product, persona). Return a JSON list of strings only."
        )
        try:
            result = provider.query(
                body, "Return JSON array of strings only.",
                user=request.user, website=None,
                audit_id="prompt_library_synthesize", role="synthesis",
                module="prompt_library",
            )
        except Exception:
            # Log the detail server-side; never echo raw provider/library
            # exception text to the client.
            logger.exception("Prompt synthesis failed")
            return Response(
                {"error": "synthesis_failed",
                 "detail": "Prompt synthesis is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if not getattr(result, "succeeded", False):
            logger.warning("Prompt synthesis unsuccessful: %s", getattr(result, "error", ""))
            return Response(
                {"error": "synthesis_failed",
                 "detail": "Prompt synthesis is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        import json
        import re

        text = getattr(result, "text", "") or ""
        match = re.search(r"\[.*\]", text, re.DOTALL)
        items: list[str] = []
        if match:
            try:
                items = [str(x) for x in json.loads(match.group()) if isinstance(x, str | int | float)]
            except Exception:
                items = []
        if not items:
            return Response(
                {"error": "synthesis_failed",
                 "detail": "Prompt synthesis is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        created = []
        for raw in items[: data["count"]]:
            envelope = auto_template(raw, user=request.user)
            template_text = envelope["template_text"].strip()
            if not template_text:
                continue
            h = text_hash(template_text)
            if Prompt.objects.filter(industry=industry, text_hash=h).exists():
                continue
            prompt = Prompt.objects.create(
                industry=industry,
                text=raw.strip(),
                template_text=template_text,
                template_variables=envelope["template_variables"],
                style=envelope["style"] or data["style"],
                intent_bucket="category",
                source=PromptSource.LLM_SYNTH,
                text_hash=h,
            )
            created.append(prompt)
        return Response({
            "created": len(created),
            "prompts": PromptSerializer(created, many=True).data,
        })


class PromptGenerateFromContextView(APIView):
    """Generate ~20 prompts from a free-form context description.

    Calls the synthesis provider (DeepSeek when configured, Anthropic
    fallback). Optionally persists the generated prompts as ``Prompt`` rows
    on the website's industry; persisting is refused (409
    ``no_matching_industry``) when the website's industry matches none.
    409 ``synthesis_not_configured`` / 502 ``synthesis_failed`` when nothing
    can be generated. Throttled per-user via ``TokenBucket`` to keep one
    user from burning the API budget.
    """

    permission_classes = [IsAuthenticated]
    _BUCKET_CAPACITY = 6
    _BUCKET_REFILL = 6 / 60.0  # 6 calls per minute, smooth refill.

    def post(self, request):
        from apps.prompt_library.services._hash import text_hash
        from apps.prompt_library.services.context_generator import (
            SynthesisFailed,
            SynthesisNotConfigured,
            generate_from_context,
        )
        from core.resilience import TokenBucket

        bucket = TokenBucket(
            name=f"prompt_ctx_gen:{request.user.id}",
            capacity=self._BUCKET_CAPACITY,
            refill_per_second=self._BUCKET_REFILL,
        )
        if not bucket.try_acquire():
            return Response(
                {"error": "rate_limited", "detail": "Too many generations. Wait a moment."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = GenerateFromContextRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        industry = None
        if data.get("persist"):
            from apps.prompt_library.services.industry_match import matching_industry

            if not data.get("website_id"):
                return Response(
                    {"error": "website_id_required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            website = WebsiteService.get_for_user(
                user=request.user, website_id=data["website_id"],
            )
            industry = matching_industry(website)
            if industry is None:
                return Response(
                    {"error": "no_matching_industry",
                     "detail": "Set the website's industry to one of the "
                               "library industries before saving prompts."},
                    status=status.HTTP_409_CONFLICT,
                )

        try:
            items, provider_name = generate_from_context(
                data["context"], count=data["count"], user=request.user,
            )
        except SynthesisNotConfigured:
            return Response(
                {"error": "synthesis_not_configured"},
                status=status.HTTP_409_CONFLICT,
            )
        except SynthesisFailed:
            return Response(
                {"error": "synthesis_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        persisted: list = []
        if industry is not None:
            for item in items:
                template_text = item["template_text"]
                h = text_hash(template_text)
                if Prompt.objects.filter(industry=industry, text_hash=h).exists():
                    continue
                prompt = Prompt.objects.create(
                    industry=industry,
                    text=item.get("preview_text") or template_text,
                    template_text=template_text,
                    template_variables=item["template_variables"],
                    style=item["style"],
                    intent_bucket=item["intent_bucket"],
                    source=PromptSource.LLM_SYNTH,
                    text_hash=h,
                )
                persisted.append(prompt)

        return Response({
            "generated": items,
            "provider": provider_name,
            "persisted": PromptSerializer(persisted, many=True).data,
        })


class PromptGenerateRelatedView(APIView):
    """Recommend prompts related to an existing one via the AI model.

    Given a single seed prompt, returns a small set of closely-related
    prompts (same topic/audience, different angle) the user can add to their
    library. Used by the create-prompt flow to surface follow-on suggestions
    immediately after a prompt is saved. Throttled per-user, sharing the same
    budget as the free-form context generator.
    """

    permission_classes = [IsAuthenticated]
    _BUCKET_CAPACITY = 6
    _BUCKET_REFILL = 6 / 60.0  # 6 calls per minute, smooth refill.

    def post(self, request):
        from apps.prompt_library.services.context_generator import (
            SynthesisFailed,
            SynthesisNotConfigured,
            generate_related_prompts,
        )
        from core.resilience import TokenBucket

        bucket = TokenBucket(
            name=f"prompt_ctx_gen:{request.user.id}",
            capacity=self._BUCKET_CAPACITY,
            refill_per_second=self._BUCKET_REFILL,
        )
        if not bucket.try_acquire():
            return Response(
                {"error": "rate_limited", "detail": "Too many generations. Wait a moment."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = GenerateRelatedRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            items, provider_name = generate_related_prompts(
                data["prompt"], count=data["count"], user=request.user,
            )
        except SynthesisNotConfigured:
            return Response(
                {"error": "synthesis_not_configured"},
                status=status.HTTP_409_CONFLICT,
            )
        except SynthesisFailed:
            return Response(
                {"error": "synthesis_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"generated": items, "provider": provider_name})


class WebsiteVariablesView(TenantScopedAPIView):
    """Get or replace the per-website prompt variable dictionary."""

    def get(self, request, website_id):
        from apps.prompt_library.services.variable_resolver import (
            variables_with_provenance,
        )

        website = self.get_website(website_id)
        var_set, _ = PromptVariableSet.objects.get_or_create(website=website)
        return Response({
            "variables": var_set.variables or {},
            "rows": variables_with_provenance(website),
        })

    def put(self, request, website_id):
        website = self.get_website(website_id)
        payload = request.data.get("variables")
        if not isinstance(payload, dict):
            return Response(
                {"error": "variables_must_be_object"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Coerce all values to strings — JSON-serialisable dicts of
        # str->str are the contract the template parser expects.
        cleaned = {str(k): ("" if v is None else str(v)) for k, v in payload.items() if k}
        var_set, _ = PromptVariableSet.objects.get_or_create(website=website)
        var_set.variables = cleaned
        var_set.save(update_fields=["variables", "updated_at"])
        return Response(PromptVariableSetSerializer(var_set).data)


class PromptToggleView(APIView):
    """Activate or deactivate a SHARED-CATALOG prompt (`/enable/` or
    `/disable/`).

    Prompt.is_active is a global catalog flag shared across every
    tenant, so this is staff-only. Per-website archiving lives on the
    tenant-owned BrandPrompt (PATCH /brand-prompts/<id>/) instead — a
    normal user archiving a prompt must not flip a row that other
    tenants also depend on.
    """

    permission_classes = [IsAdminUser]

    def post(self, request, prompt_id, action: str):
        prompt = get_object_or_404(Prompt, id=prompt_id)
        if action == "enable":
            prompt.is_active = True
        elif action == "disable":
            prompt.is_active = False
        else:
            return Response({"error": "unknown_action"}, status=400)
        prompt.save(update_fields=["is_active", "updated_at"])
        return Response(PromptSerializer(prompt).data)


def _build_alignment_summary(website, answered_results) -> dict:
    """Aggregate per-response brand-alignment into the prompt-page block.

    ``state`` tells the UI which empty/cold-start copy to render:
    scored | extraction_pending (scored against chunks only, no facts
    yet) | no_brand_input | embeddings_unavailable | none (nothing
    analyzed or nothing brand-scoped to score).
    """
    scores: list[float] = []
    statuses: set[str] = set()
    reflected_counts: dict[str, int] = {}
    missing_counts: dict[str, int] = {}
    unsupported_samples: list[dict] = []
    bases: set[str] = set()
    analyzed = 0

    for r in answered_results:
        detail = r.alignment_detail or {}
        status_str = detail.get("status") or ""
        if status_str:
            analyzed += 1
            statuses.add(status_str)
        if r.alignment_score is not None:
            scores.append(r.alignment_score)
            bases.add(detail.get("basis") or "")
            coverage = detail.get("coverage") or {}
            for item in coverage.get("reflected") or []:
                text = (item or {}).get("text") or ""
                if text:
                    reflected_counts[text] = reflected_counts.get(text, 0) + 1
            for item in coverage.get("missing") or []:
                text = (item or {}).get("text") or ""
                if text:
                    missing_counts[text] = missing_counts.get(text, 0) + 1
            if len(unsupported_samples) < 3:
                for sample in (detail.get("support") or {}).get("unsupported_samples") or []:
                    text = (sample or {}).get("text") or ""
                    if text:
                        unsupported_samples.append({"text": text, "provider": r.provider})
                        break

    def _top(counts: dict[str, int]) -> list[dict]:
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return [{"text": text, "count": count} for text, count in ranked]

    if scores:
        state = "scored"
        if bases == {"chunks_only"}:
            from apps.brand_vault.models import BrandFact, FactStatus
            has_facts = BrandFact.objects.filter(
                website=website,
                status__in=(FactStatus.APPROVED, FactStatus.AUTO),
                version_to__isnull=True,
            ).exists()
            if not has_facts:
                state = "extraction_pending"
    elif "embeddings_unavailable" in statuses:
        state = "embeddings_unavailable"
    elif "no_brand_input" in statuses:
        state = "no_brand_input"
    elif analyzed == 0 and answered_results:
        state = "none"
    else:
        # Analyzed but nothing scoreable (answers never talked about the
        # brand) or no responses at all.
        state = "none"

    return {
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "scored": len(scores),
        "total": len(answered_results),
        "state": state,
        "top_reflected": _top(reflected_counts),
        "top_missing": _top(missing_counts),
        "unsupported_samples": unsupported_samples,
    }


class BrandPromptDetailAggView(TenantScopedAPIView):
    """
    Per-prompt analytics drilldown — what every model said about the
    brand when asked this exact prompt, aggregated across every audit
    that has run it on this website.

    Returns:
      - prompt: text, intent, topic, source_score, runs_count
      - status: "active" / "inactive" based on Prompt.is_active and
        whether the brand currently has it via BrandPrompt
      - per-brand visibility, sov, sentiment, position
      - top domains cited in matching responses with citation rates
      - domain type breakdown
    """

    def get(self, request, website_id, prompt_id):
        from collections import Counter, defaultdict

        from apps.citations.models import Citation
        from apps.prompt_library.services.prompt_stats import (
            aggregate_by_model,
            matching_results,
        )

        website = self.get_website(website_id)

        try:
            prompt = Prompt.objects.select_related("industry").get(id=prompt_id)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)

        bp = BrandPrompt.objects.filter(website=website, prompt=prompt).first()
        bp_exists = bp is not None
        bp_location = bp.location if bp else ""
        bp_tags = bp.tags if bp else []

        # Optional time-series controls for the Overview:
        #   ?run=<audit_id>            → drill into a single run (else all)
        #   ?period=7d|30d|90d|1y|all  → window the results (default all)
        selected_run = (request.query_params.get("run") or "").strip() or None
        period = (request.query_params.get("period") or "all").strip().lower()

        # Match every LLMRankingResult on this website that answered this
        # prompt (source_prompt FK or exact text; see prompt_stats).
        # Window by created_at when a period is requested. Applied to the
        # queryset so both the trend series and the per-model aggregation
        # below reflect the same window.
        _PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
        _period_days = _PERIOD_DAYS.get(period)
        since = None
        if _period_days:
            from datetime import timedelta as _td

            from django.utils import timezone as _tz
            since = _tz.now() - _td(days=_period_days)

        results = matching_results(
            website, prompt, since=since,
            fields=(
                "id", "public_id", "provider", "prompt", "response_text",
                "is_mentioned", "mention_rank", "sentiment",
                "competitors_mentioned", "created_at",
                "citation_countries", "query_succeeded", "error_message",
                "extraction_model", "source_prompt_id", "audit_id",
                "alignment_score", "alignment_detail",
            ),
        )

        # Visibility is "in what share of real answers does this brand
        # appear", so the denominator must be the responses that actually
        # returned text - not failed/unavailable cells, which would dilute
        # every brand's score.
        answered_results = [
            r for r in results
            if r.query_succeeded and (r.response_text or "").strip()
        ]
        num_answers = len(answered_results)

        # ── Per-run time-series (trend across runs) ───────────────────
        # Group the period-scoped answered responses by audit_id: each
        # crawl/audit that included this prompt is one "run" (a per-prompt
        # crawl spawns its own synthetic audit). Built BEFORE the optional
        # single-run drill-in below, so selecting a run never collapses the
        # trend. Self-brand visibility mirrors the headline metric
        # (is_mentioned / answered), sentiment on the same 0..100 map.
        _SENTIMENT_SCORE = {"positive": 85, "neutral": 55, "negative": 25}
        _run_buckets: dict[str, dict] = {}
        for r in answered_results:
            rkey = str(r.audit_id) if r.audit_id else "none"
            b = _run_buckets.get(rkey)
            if b is None:
                b = {
                    "run_id": rkey, "ran_at": r.created_at, "answers": 0,
                    "self_mentions": 0, "sentiments": [], "positions": [],
                    "alignments": [],
                }
                _run_buckets[rkey] = b
            b["answers"] += 1
            if r.created_at and (b["ran_at"] is None or r.created_at < b["ran_at"]):
                b["ran_at"] = r.created_at
            if r.alignment_score is not None:
                b["alignments"].append(r.alignment_score)
            if r.is_mentioned:
                b["self_mentions"] += 1
                if r.sentiment and r.sentiment != "not_mentioned":
                    b["sentiments"].append(_SENTIMENT_SCORE.get(r.sentiment, 50))
                if r.mention_rank is not None:
                    b["positions"].append(r.mention_rank)
        runs = []
        for b in _run_buckets.values():
            ans = b["answers"] or 1
            runs.append({
                "run_id": b["run_id"],
                "ran_at": b["ran_at"].isoformat() if b["ran_at"] else None,
                "answers": b["answers"],
                "visibility_pct": round(100 * b["self_mentions"] / ans, 1),
                "sentiment_score": (
                    round(sum(b["sentiments"]) / len(b["sentiments"]), 1)
                    if b["sentiments"] else None
                ),
                "avg_position": (
                    round(sum(b["positions"]) / len(b["positions"]), 1)
                    if b["positions"] else None
                ),
                "alignment_avg": (
                    round(sum(b["alignments"]) / len(b["alignments"]), 1)
                    if b["alignments"] else None
                ),
            })
        runs.sort(key=lambda x: x["ran_at"] or "")

        # ── Optional drill-in: scope everything below to one run ──────
        if selected_run:
            results = [r for r in results if str(r.audit_id) == selected_run]
            answered_results = [
                r for r in answered_results if str(r.audit_id) == selected_run
            ]
            num_answers = len(answered_results)

        # Responses that still need (re)extraction - never analysed, or
        # analysed by the weak heuristic / an older schema. The UI uses this
        # to trigger a one-shot backfill so the brand table fills in
        # accurately.
        from apps.prompt_library.services.reextract import count_unextracted
        unextracted_count = count_unextracted(website, prompt)

        # ── Per-brand visibility / SOV / sentiment / position ──────────
        brand_label = getattr(website, "business_name", None) or website.name or "your brand"
        brand_label_lower = brand_label.lower()

        brand_stats: dict[str, dict] = {}

        def _ensure(name: str) -> dict:
            key = name.strip().lower()
            if not key:
                return {}
            row = brand_stats.get(key)
            if row is None:
                row = {
                    "key": key,
                    "name": name.strip(),
                    "mentions": 0,
                    "responses_with_mention": 0,
                    "sentiments": [],
                    "positions": [],
                    "providers": set(),
                    "domain": "",
                }
                brand_stats[key] = row
            return row

        from apps.citations.services.url_analytics import (
            extract_response_brands,
            model_key,
        )

        for r in answered_results:
            # Collect every brand named in THIS response, deduped, keeping
            # the best (lowest) position. Three sources, in priority order:
            #   1. the tracked brand (when the extractor flagged it)
            #   2. extracted competitors_mentioned
            #   3. brands parsed straight from the response's numbered list
            # The third is the fallback that makes the table populate even
            # when extraction was sparse but the model plainly listed brands.
            per_result: dict[str, dict] = {}

            def _see(name, position, is_self=False, sentiment=None,
                     position_authoritative=False, domain=None, *, _pr=per_result):
                name = (name or "").strip()
                if not name:
                    return
                key = name.lower()
                slot = _pr.get(key)
                if slot is None:
                    _pr[key] = {
                        "name": name, "position": position,
                        "is_self": is_self, "sentiment": sentiment,
                        "domain": domain or "",
                    }
                else:
                    if is_self:
                        slot["is_self"] = True
                    if position is not None:
                        # The order a brand appears in the response (text-parse,
                        # position_authoritative) is the ground-truth rank, so
                        # it overrides the extractor's noisier position. Other
                        # sources only fill a missing position.
                        if position_authoritative or slot["position"] is None:
                            slot["position"] = position
                    if sentiment and not slot.get("sentiment"):
                        slot["sentiment"] = sentiment
                    if domain and not slot.get("domain"):
                        slot["domain"] = domain

            if r.is_mentioned:
                _see(
                    brand_label, r.mention_rank, is_self=True,
                    sentiment=r.sentiment if r.sentiment != "not_mentioned" else None,
                )
            for comp in (r.competitors_mentioned or []):
                if isinstance(comp, dict):
                    # Default to neutral when the row predates per-competitor
                    # sentiment (v2); a v2 re-extract replaces it with the
                    # real positive/negative tone. domain (v3) is the brand's
                    # website the model named - used only to crawl its logo.
                    _see(
                        comp.get("name"), comp.get("position"),
                        sentiment=comp.get("sentiment") or "neutral",
                        domain=comp.get("domain") or "",
                    )
                elif isinstance(comp, str):
                    _see(comp, None, sentiment="neutral")
            if (r.response_text or "").strip():
                for entry in extract_response_brands(r.response_text):
                    # The response's actual list order is the authoritative
                    # rank, overriding the extractor's position so brands get a
                    # clean 1,2,3,... within a single answer. Sentiment stays
                    # whatever the extractor assigned (neutral only fills gaps).
                    _see(
                        entry["name"], entry.get("position"),
                        sentiment="neutral", position_authoritative=True,
                    )

            for info in per_result.values():
                row = _ensure(info["name"])
                if not row:
                    continue
                row["responses_with_mention"] += 1
                row["mentions"] += 1
                row["providers"].add(r.provider)
                if info["position"] is not None:
                    row["positions"].append(info["position"])
                sent = info.get("sentiment")
                if sent and sent != "not_mentioned":
                    row["sentiments"].append(sent)
                if info.get("domain") and not row.get("domain"):
                    row["domain"] = info["domain"]

        total_mentions = sum(row["mentions"] for row in brand_stats.values()) or 1
        brands_out = []
        for row in brand_stats.values():
            avg_pos = round(sum(row["positions"]) / len(row["positions"]), 1) if row["positions"] else None
            # Map sentiment strings to a 0..100 scale for the display:
            # positive=85, neutral=55, negative=25. Averages produce a
            # rough indicator the UI shows as a small "•" pill value.
            smap = {"positive": 85, "neutral": 55, "negative": 25}
            scores = [smap.get(s, 50) for s in row["sentiments"]]
            sentiment_score = round(sum(scores) / len(scores), 1) if scores else None
            vis_pct = round(100 * row["responses_with_mention"] / num_answers, 1) if num_answers else 0.0
            sov_pct = round(100 * row["mentions"] / total_mentions, 1)
            brands_out.append({
                "name": row["name"],
                "is_self": row["key"] == brand_label_lower,
                "visibility_pct": vis_pct,
                "sov_pct": sov_pct,
                "sentiment_score": sentiment_score,
                "avg_position": avg_pos,
                "mentions": row["mentions"],
                # Which models named this brand (mapped to UI model keys),
                # and how many - a brand named by more models is stronger.
                "models": sorted(model_key(p) for p in row["providers"]),
                "model_count": len(row["providers"]),
                # LLM-supplied website domain (used only to crawl the logo).
                "_llm_domain": row.get("domain", ""),
            })
        # Most-visible first; break ties by share of voice, then by best
        # average rank (lower is better) so a brand the models put at #1
        # outranks one they bury, even when both appear equally often.
        brands_out.sort(key=lambda x: (
            -x["visibility_pct"],
            -x["sov_pct"],
            x["avg_position"] if x["avg_position"] is not None else 999,
        ))

        # ── Top domains + type breakdown from citations on these results
        result_ids = [r.id for r in results]
        citations_qs = Citation.objects.filter(result_id__in=result_ids).only(
            "apex_domain", "domain", "source_class", "result_id",
        )
        per_domain: dict[str, dict] = {}
        responses_with_citation: dict[str, set] = defaultdict(set)
        sources_by_result: dict[str, list] = defaultdict(list)
        for c in citations_qs:
            apex = c.apex_domain or c.domain
            if not apex:
                continue
            entry = per_domain.setdefault(apex, {
                "domain": c.domain,
                "apex_domain": apex,
                "source_class": c.source_class,
                "count": 0,
            })
            entry["count"] += 1
            responses_with_citation[apex].add(c.result_id)
            sources_by_result[c.result_id].append(apex)

        # Resolve a website domain per brand so the UI can crawl its logo.
        # Prefer the domain the extractor (LLM) supplied for the brand, then
        # fall back to matching the name against a domain the models cited.
        # The domain is used only to fetch the logo - never shown.
        cited_domains = list(per_domain.keys())
        for b in brands_out:
            domain = b.pop("_llm_domain", "") or _match_brand_domain(b["name"], cited_domains)
            b["domain"] = domain or ""

        # public_id is what the chat modal opens — map internal id -> public.
        public_id_by_result = {r.id: str(r.public_id) for r in results}

        # Brand sentiment per citing answer, so each domain (and each domain
        # type) can report "how positively do answers that lean on this
        # source talk about the brand". Derived entirely from the stored
        # responses -- the domain's own pages are never fetched for this.
        _DOMAIN_SMAP = {"positive": 85, "neutral": 55, "negative": 25}
        sentiment_by_result = {
            r.id: _DOMAIN_SMAP.get(r.sentiment, 55)
            for r in answered_results
            if r.is_mentioned and r.sentiment and r.sentiment != "not_mentioned"
        }

        top_domains = []
        for apex, entry in per_domain.items():
            unique_responses = len(responses_with_citation[apex])
            # Denominator is ANSWERED responses (same philosophy as
            # visibility): a failed cell can't cite anything, so counting it
            # would dilute every domain's share. answers_citing/answers_total
            # let the UI say "cited in 1 of 3 answers" in plain words.
            entry["answers_citing"] = unique_responses
            entry["answers_total"] = num_answers
            entry["citation_rate"] = (
                round(unique_responses / num_answers, 2) if num_answers else 0.0
            )
            entry["retrieved_pct"] = (
                round(100 * unique_responses / num_answers, 1) if num_answers else 0.0
            )
            _sents = [
                sentiment_by_result[rid]
                for rid in responses_with_citation[apex]
                if rid in sentiment_by_result
            ]
            # None when the brand never appeared in an answer citing this
            # domain -- the UI shows a dash, never an invented neutral.
            entry["brand_sentiment"] = (
                round(sum(_sents) / len(_sents), 1) if _sents else None
            )
            entry["brand_mentions_citing"] = len(_sents)
            sample_ids = [
                public_id_by_result[rid]
                for rid in list(responses_with_citation[apex])[:8]
                if rid in public_id_by_result
            ]
            entry["sample_result_ids"] = sample_ids
            top_domains.append(entry)
        top_domains.sort(key=lambda e: (-e["count"], e["apex_domain"]))
        top_domains = top_domains[:25]

        # ── Page-level sentiment from the latest seeded SourceScan ────
        # "Scan cited sources" fetches the cited pages and scores how the
        # brand is portrayed ON the page (vs. brand_sentiment above, which
        # scores the AI answers that cite it). Sentiment arrives -1..1 from
        # the intelligence service; map onto the UI's 0..100 scale.
        from apps.citations.models import SourceScan, SourceScanStatus
        from apps.citations.services.source_scan import _matches_target
        from apps.citations.services.url_normalizer import extract_apex_domain

        page_scan = None
        latest_scan_row = (
            SourceScan.objects
            .filter(website=website, source_prompt=prompt)
            .order_by("-created_at")
            .first()
        )
        if latest_scan_row is not None:
            page_scan = {
                "id": str(latest_scan_row.id),
                "status": latest_scan_row.status,
                "analyzed_count": latest_scan_row.analyzed_count,
                "results_count": latest_scan_row.results_count,
                "error": _public_scan_error(latest_scan_row.error),
                "created_at": latest_scan_row.created_at.isoformat()
                if latest_scan_row.created_at else None,
                "completed_at": latest_scan_row.completed_at.isoformat()
                if latest_scan_row.completed_at else None,
            }
            if latest_scan_row.status == SourceScanStatus.COMPLETE:
                brand_name = website.name or ""
                page_sent: dict[str, list] = defaultdict(list)
                # Overall tone of each page across EVERY brand/product it
                # discusses (not just ours), weighted by how prominently
                # each features. Drives the Source-tone card: "is the
                # content AI cites for this prompt positive or negative".
                page_tone: dict[str, list] = defaultdict(list)
                page_seen: set[str] = set()
                for res in latest_scan_row.results.filter(relevant=True):
                    apex = extract_apex_domain(res.domain) or res.domain
                    if res.fetch_status == "ok":
                        page_seen.add(apex)
                    tone_num = tone_den = 0.0
                    for b in res.brands or []:
                        raw = b.get("sentiment")
                        if raw is None:
                            continue
                        if _matches_target(b.get("name", ""), brand_name):
                            page_sent[apex].append(round((float(raw) + 1) * 50))
                        w = max(float(b.get("weight") or 0.0), 0.1)
                        tone_num += float(raw) * w
                        tone_den += w
                    if tone_den:
                        page_tone[apex].append(
                            round((tone_num / tone_den + 1) * 50)
                        )
                for entry in top_domains:
                    apex = entry["apex_domain"]
                    vals = page_sent.get(apex)
                    entry["page_sentiment"] = (
                        round(sum(vals) / len(vals), 1) if vals else None
                    )
                    tones = page_tone.get(apex)
                    entry["page_tone"] = (
                        round(sum(tones) / len(tones), 1) if tones else None
                    )
                    # Distinguishes "page analyzed, brand absent" from
                    # "page was never fetched/analyzed".
                    entry["page_analyzed"] = apex in page_seen

        type_counter: Counter = Counter()
        type_sentiments: dict[str, list] = defaultdict(list)
        for apex, entry in per_domain.items():
            tkey = entry["source_class"] or "other"
            type_counter[tkey] += entry["count"]
            # Same answer-derived sentiment, rolled up per source type: "do
            # answers grounded in UGC talk about the brand differently from
            # answers grounded in editorial sources".
            type_sentiments[tkey].extend(
                sentiment_by_result[rid]
                for rid in responses_with_citation[apex]
                if rid in sentiment_by_result
            )
        total_citations = sum(type_counter.values()) or 1
        type_breakdown = [
            {
                "key": key,
                "count": cnt,
                "pct": round(100 * cnt / total_citations, 1),
                "brand_sentiment": (
                    round(sum(type_sentiments[key]) / len(type_sentiments[key]), 1)
                    if type_sentiments[key] else None
                ),
            }
            for key, cnt in type_counter.most_common()
        ]

        # ── Brand visibility per model for this prompt ──
        by_model = aggregate_by_model(results)

        ordered = sorted(
            results, key=lambda r: r.created_at or prompt.created_at, reverse=True,
        )[:100]
        recent_chats = []
        for r in ordered:
            srcs, seen = [], set()
            for d in sources_by_result.get(r.id, []):
                if d and d not in seen:
                    seen.add(d)
                    srcs.append(d)
            cc = r.citation_countries or {}
            country = max(cc.items(), key=lambda kv: kv[1])[0] if isinstance(cc, dict) and cc else None
            has_response = bool((r.response_text or "").strip())
            err = getattr(r, "error_message", "") or ""
            # Distinguish a cell that returned text (complete) from one that
            # errored. A failed cell with the service_unavailable sentinel is
            # surfaced separately from a genuine upstream error, and only a
            # cell that succeeded-but-empty (still in flight) reads "pending".
            if has_response:
                chat_status = "complete"
            elif err.startswith("service_unavailable"):
                chat_status = "unavailable"
            elif not r.query_succeeded:
                chat_status = "failed"
            else:
                chat_status = "pending"
            recent_chats.append({
                "result_id": str(r.public_id),
                "prompt": r.prompt,
                "response_preview": (r.response_text or "")[:400],
                "brand_mentioned": r.is_mentioned,
                "position": r.mention_rank,
                "alignment": (
                    round(r.alignment_score)
                    if r.alignment_score is not None else None
                ),
                "models": [model_key(r.provider)],
                "provider": r.provider,
                "model_id": getattr(r, "model_id", "") or "",
                "sources": srcs[:6],
                "country": country,
                "status": chat_status,
                # Sanitized: the raw provider error stays in the DB/logs.
                "error": (
                    _public_model_error(err)
                    if chat_status in ("failed", "unavailable") else ""
                ),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

        # ── Latest crawl run for this (website, prompt) ────────────────
        # Surfaces "scanning now / last scan succeeded / last scan failed"
        # in the page header so the empty state has an actionable handle.
        #
        # Self-heal stale runs: the crawl is synchronous, so a run left in
        # "running"/"pending" well past STALE_RUN_MINUTES means the worker
        # or request died mid-scan (it is NOT still hitting the model APIs).
        # Flip it to failed on read so the UI stops showing a forever-
        # ticking "Scan running" and the user can re-trigger.
        from datetime import timedelta

        from django.utils import timezone

        from apps.prompt_library.models import PromptCrawlRun

        STALE_RUN_MINUTES = 10

        latest_run = (
            PromptCrawlRun.objects
            .filter(website=website, prompt=prompt)
            .order_by("-created_at")
            .first()
        )
        if (
            latest_run is not None
            and latest_run.status in (PromptCrawlRun.STATUS_RUNNING, PromptCrawlRun.STATUS_PENDING)
        ):
            anchor = latest_run.started_at or latest_run.created_at
            if anchor and (timezone.now() - anchor) > timedelta(minutes=STALE_RUN_MINUTES):
                latest_run.status = PromptCrawlRun.STATUS_FAILED
                latest_run.error = (latest_run.error or "") or "Scan timed out and was stopped."
                latest_run.completed_at = timezone.now()
                latest_run.save(update_fields=["status", "error", "completed_at"])

        latest_scan = None
        if latest_run is not None:
            latest_scan = {
                "id": str(latest_run.id),
                "status": latest_run.status,
                "providers": latest_run.providers or [],
                "started_at": latest_run.started_at.isoformat() if latest_run.started_at else None,
                "completed_at": latest_run.completed_at.isoformat() if latest_run.completed_at else None,
                # Sanitized: raw run errors (provider messages, key state)
                # stay server-side.
                "error": _public_scan_error(latest_run.error),
            }

        return Response({
            "prompt": {
                "id": str(prompt.id),
                "text": prompt.text,
                "template_text": prompt.template_text or "",
                "intent_bucket": prompt.intent_bucket,
                "topic": prompt.industry.name if prompt.industry_id else "",
                "location": bp_location,
                "tags": bp_tags,
                # No demand signal exists; source_score is the library's
                # ranking score (source type, recency, paraphrase count).
                "demand_score": None,
                "source_score": prompt.demand_score,
                "runs_count": prompt.runs_count,
                "effectiveness_score": prompt.effectiveness_score,
                "created_at": prompt.created_at.isoformat() if prompt.created_at else None,
            },
            "is_brand_prompt": bp_exists,
            "status": "active" if (prompt.is_active and bp_exists) else "inactive",
            "total_responses": num_answers,
            "brand_label": brand_label,
            "brands": brands_out,
            "by_model": by_model,
            "top_domains": top_domains,
            "domain_types": type_breakdown,
            "total_retrievals": sum(type_counter.values()),
            # How closely this prompt's answers reflect the brand's own
            # material, aggregated across the window's responses.
            "brand_alignment": _build_alignment_summary(website, answered_results),
            "recent_chats": recent_chats,
            "latest_scan": latest_scan,
            # Latest "Scan cited sources" run for this prompt (page-level
            # sentiment), or null when none has been started.
            "page_scan": page_scan,
            "unextracted_count": unextracted_count,
            # Time-series: one entry per run (audit) that included this
            # prompt, ascending by time — drives the Overview trend chart
            # and the run selector. ``selected_run``/``period`` echo the
            # applied filters.
            "runs": runs,
            "selected_run": selected_run,
            "period": period,
        })


class PromptReextractView(TenantScopedAPIView):
    """Backfill brand extraction onto a prompt's existing responses.

    Re-runs the structured extractor on rows that have text but were
    never analysed (e.g. captured by an older crawler), so the brand
    table fills in without re-querying the models. Idempotent: rows
    already extracted are skipped, so repeated calls are cheap no-ops.
    """

    def post(self, request, website_id, prompt_id):
        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)

        from apps.prompt_library.services.reextract import reextract_prompt
        updated = reextract_prompt(website, prompt)
        return Response({"reextracted": updated})


class BrandLogoView(APIView):
    """Resolve a website's logo by crawling its domain.

    GET ?domain=tasselsbyrenu.com -> {"domain": ..., "logo": "https://..."}.
    Results are cached server-side; the crawl fetches the homepage and
    extracts the best logo (apple-touch-icon / og:image / icon / favicon).
    Returns logo="" when nothing usable is found so the UI can fall back.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.prompt_library.services.logo_crawler import (
            fetch_site_logo,
            normalise_domain,
        )

        raw = request.query_params.get("domain", "")
        domain = normalise_domain(raw)
        if not domain:
            return Response({"detail": "Invalid domain."}, status=400)
        return Response({"domain": domain, "logo": fetch_site_logo(domain)})


# Caps for the per-prompt rollups on the saved-prompts table. The UI shows
# a couple of chips per cell and a "+N" overflow, so sending the long tail
# would only inflate the payload.
TOP_COMPETITORS = 6
TOP_DOMAINS = 5


def _competitor_names(raw) -> list[str]:
    """Names out of LLMRankingResult.competitors_mentioned.

    The column is JSON written by several extractor generations, so entries
    are sometimes plain strings and sometimes {name, position, linked}
    dicts. Anything else is skipped rather than raising -- a malformed row
    must not take down the whole table.
    """
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        name = ""
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("brand") or ""
        name = str(name).strip()
        if name:
            out.append(name[:80])
    return out


def _citation_domains(raw) -> list[str]:
    """Display domains out of LLMRankingResult.citations.

    Same tolerance as above: entries may be bare URL strings or dicts.
    Routing through normalize_url also drops non-http(s) schemes, which
    matters because these become clickable links downstream.
    """
    from apps.citations.services.url_normalizer import normalize_url

    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        url = ""
        if isinstance(entry, str):
            url = entry
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("link") or entry.get("href") or ""
        if not url:
            continue
        _, host, _ = normalize_url(str(url))
        if not host:
            continue
        out.append((host[4:] if host.startswith("www.") else host)[:120])
    return out


class WebsiteSavedPromptsAggView(TenantScopedAPIView):
    """
    Saved-prompts dashboard payload — every saved prompt for the
    website plus aggregated metrics ready for the Saved tab table
    (visibility %, sentiment, position, models that mentioned, runs
    count, intent/style tags, volume bucket, topic).

    Heavier than the brand-prompts list endpoint but materialised in a
    single query pass plus a per-prompt response scan. Topics are
    derived from each prompt's industry name; intent + style are
    surfaced as tag chips.
    """

    # The saved list is never auto-populated. Audits run exactly what the
    # user saved, so a seeded list would silently decide what gets measured
    # on their behalf. A fresh website lands on an empty Active tab; the
    # Suggested tab is where library prompts are offered for explicit
    # adoption. (An autoseed used to copy 12 library prompts here on first
    # visit — removed 2026-08.)

    def get(self, request, website_id):
        from collections import Counter, defaultdict

        from apps.llm_ranking.models import LLMRankingResult

        website = self.get_website(website_id)

        bps = (
            BrandPrompt.objects
            .filter(website=website)
            .select_related("prompt", "prompt__industry")
            .order_by("-created_at")
        )
        prompts_by_norm: dict[str, list] = defaultdict(list)
        rows = []
        for bp in bps:
            p = bp.prompt
            norm = " ".join((p.text or "").split()).lower()
            prompts_by_norm[norm].append(bp.id)
            rows.append({
                "_norm": norm,
                "id": str(p.id),
                "brand_prompt_id": str(bp.id),
                "text": p.text,
                "template_text": p.template_text or "",
                "topic": p.industry.name if p.industry_id else "",
                "topic_slug": p.industry.slug if p.industry_id else "",
                "intent_bucket": p.intent_bucket,
                "style": p.style,
                "tags": bp.tags or [],
                "location": bp.location or "",
                "demand_score": None,
                "source_score": p.demand_score,
                "runs_count": p.runs_count,
                "is_active": p.is_active,
                # Per-website archive state (drives the Active/Archived
                # split); distinct from the global catalog p.is_active.
                "is_archived": bp.is_archived,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                # None until an engine has answered this prompt.
                "visibility_pct": None,
                "sentiment_score": None,
                "avg_position": None,
                "models_mentioned": [],
                "total_mentions": 0,
                "responses_seen": 0,
                # Cached 0-1 effectiveness, already on the select_related
                # Prompt -- free to expose.
                "effectiveness_score": p.effectiveness_score,
                # Who else the models name on this prompt, most-cited first.
                "top_competitors": [],
                "competitors_count": 0,
                # What the models cite when answering it.
                "citations_count": 0,
                "top_domains": [],
                # Per-provider visibility, so a prompt that only lands on one
                # engine is distinguishable from one that lands everywhere.
                "by_engine": [],
                # Raw counts behind sentiment_score, which is only a mean.
                "sentiment_dist": {"positive": 0, "neutral": 0, "negative": 0},
                # Scheduling: last_run_at = newest result timestamp (any
                # audit/crawl that ran this prompt); next_run_at/frequency
                # come from an enabled PromptSchedule, else null → the UI
                # shows "Not scheduled".
                "last_run_at": None,
                "next_run_at": None,
                "schedule_frequency": None,
            })

        # Pull every LLMRankingResult for this website's audits once.
        # Then bucket per normalised prompt text — fast even with
        # thousands of results because we hold them in memory.
        all_results = list(
            LLMRankingResult.objects
            .filter(audit__website=website)
            .only(
                "provider", "prompt", "is_mentioned", "mention_rank",
                "sentiment", "created_at", "query_succeeded",
                # Both are JSON columns already on the row; loading them
                # here is what lets the loop below build the competitor and
                # citation rollups without a second query.
                "competitors_mentioned", "citations",
            ),
        )

        smap = {"positive": 85, "neutral": 55, "negative": 25}
        agg: dict[str, dict] = {}
        for r in all_results:
            key = " ".join((r.prompt or "").split()).lower()
            if key not in prompts_by_norm:
                continue
            entry = agg.setdefault(key, {
                "responses": 0,
                "mentioned": 0,
                "positions": [],
                "sentiments": [],
                "providers_mentioned": set(),
                "last_run": None,
                "competitors": Counter(),
                "domains": Counter(),
                "citations": 0,
                # provider -> [responses, mentioned]
                "by_provider": defaultdict(lambda: [0, 0]),
            })
            if r.created_at and (entry["last_run"] is None or r.created_at > entry["last_run"]):
                entry["last_run"] = r.created_at
            # A failed call is a run but not an answer: it counts toward
            # last_run_at only.
            if not r.query_succeeded:
                continue
            entry["responses"] += 1
            if r.provider:
                entry["by_provider"][r.provider][0] += 1

            # Competitors and citations are recorded for every response, not
            # only the ones that mention us: a prompt we are absent from
            # still tells us who owns it and what it cites, which is exactly
            # the gap worth acting on.
            for name in _competitor_names(r.competitors_mentioned):
                entry["competitors"][name] += 1
            for domain in _citation_domains(r.citations):
                entry["domains"][domain] += 1
                entry["citations"] += 1

            if r.is_mentioned:
                entry["mentioned"] += 1
                if r.provider:
                    entry["by_provider"][r.provider][1] += 1
                if r.mention_rank is not None:
                    entry["positions"].append(r.mention_rank)
                if r.sentiment and r.sentiment != "not_mentioned":
                    entry["sentiments"].append(r.sentiment)
                if r.provider:
                    entry["providers_mentioned"].add(r.provider)

        for row in rows:
            stats = agg.get(row["_norm"])
            if not stats:
                continue
            responses = stats["responses"]
            if responses:
                row["responses_seen"] = responses
                row["visibility_pct"] = round(100 * stats["mentioned"] / responses, 0)
            row["total_mentions"] = stats["mentioned"]
            if stats["positions"]:
                row["avg_position"] = round(sum(stats["positions"]) / len(stats["positions"]), 1)
            if stats["sentiments"]:
                scores = [smap.get(s, 50) for s in stats["sentiments"]]
                row["sentiment_score"] = round(sum(scores) / len(scores), 0)
            if stats["sentiments"]:
                dist = Counter(stats["sentiments"])
                row["sentiment_dist"] = {
                    "positive": dist.get("positive", 0),
                    "neutral": dist.get("neutral", 0),
                    "negative": dist.get("negative", 0),
                }
            row["models_mentioned"] = sorted(stats["providers_mentioned"])
            row["competitors_count"] = len(stats["competitors"])
            row["top_competitors"] = [
                {"name": name, "count": count}
                for name, count in stats["competitors"].most_common(TOP_COMPETITORS)
            ]
            row["citations_count"] = stats["citations"]
            row["top_domains"] = [
                {"domain": domain, "count": count}
                for domain, count in stats["domains"].most_common(TOP_DOMAINS)
            ]
            row["by_engine"] = [
                {
                    "provider": provider,
                    "responses": seen,
                    "mentioned": hit,
                    "visibility_pct": round(100 * hit / seen, 0) if seen else 0,
                }
                for provider, (seen, hit) in sorted(stats["by_provider"].items())
            ]
            if stats.get("last_run"):
                row["last_run_at"] = stats["last_run"].isoformat()

        # Overlay per-prompt schedule state (Next run column). One query;
        # only enabled schedules count as "scheduled".
        schedules = {
            str(bp_id): (next_run, freq)
            for bp_id, next_run, freq in PromptSchedule.objects.filter(
                brand_prompt__website=website, is_enabled=True,
            ).values_list("brand_prompt_id", "next_run_at", "frequency")
        }
        for row in rows:
            sched = schedules.get(row["brand_prompt_id"])
            if sched:
                next_run, freq = sched
                row["next_run_at"] = next_run.isoformat() if next_run else None
                row["schedule_frequency"] = freq

        # Drop the helper key before responding.
        for row in rows:
            row.pop("_norm", None)

        # Tag summary (over the full set, so the filter list is stable) and
        # optional ?tag= filter applied to the rows.
        tag_counter: Counter = Counter()
        for row in rows:
            for t in (row.get("tags") or []):
                tag_counter[t] += 1
        all_tags = [
            {"name": name, "count": count}
            for name, count in tag_counter.most_common()
        ]
        tag_filter = (request.query_params.get("tag") or "").strip()
        if tag_filter:
            rows = [r for r in rows if tag_filter in (r.get("tags") or [])]

        # Topic summary for the left rail.
        topic_counter: Counter = Counter()
        for row in rows:
            t = row.get("topic") or ""
            if t:
                topic_counter[t] += 1
        topics = [
            {"name": name, "count": count}
            for name, count in topic_counter.most_common()
        ]

        # Aggregate KPI for the strip above the table.
        with_vis = [r for r in rows if r["responses_seen"]]
        avg_vis = round(sum(r["visibility_pct"] for r in with_vis) / len(with_vis), 0) if with_vis else None
        with_sent = [r for r in rows if r["sentiment_score"] is not None]
        avg_sent = round(sum(r["sentiment_score"] for r in with_sent) / len(with_sent), 0) if with_sent else None
        with_pos = [r for r in rows if r["avg_position"] is not None]
        avg_pos = round(sum(r["avg_position"] for r in with_pos) / len(with_pos), 1) if with_pos else None

        return Response({
            "website_id": str(website.id),
            "rows": rows,
            "total": len(rows),
            "topics": topics,
            "all_tags": all_tags,
            "kpi": {
                "visibility_pct": avg_vis,
                "sentiment_score": avg_sent,
                "avg_position": avg_pos,
            },
        })


class WebsiteSuggestedPromptsView(TenantScopedAPIView):
    """
    Suggested-prompts view for the Prompt-library dashboard's
    'Suggested' subtab.

    Returns library prompts that:
      - Are active
      - Match the website's industry (no suggestions when it matches none)
      - Are NOT already saved as BrandPrompts on this website
      - Have NOT been explicitly rejected via the × button

    Each row carries the same shape the saved-aggregate endpoint
    returns minus the per-prompt response metrics — visibility /
    sentiment / position aren't computed because suggestions haven't
    been audited yet.
    """

    LIMIT = 25

    def get(self, request, website_id):
        from apps.prompt_library.services.industry_match import matching_industry

        website = self.get_website(website_id)

        industry = matching_industry(website)
        if industry is None:
            return Response({
                "website_id": str(website.id),
                "rows": [],
                "topics": [],
                "total": 0,
                "reason": "no_matching_industry",
            })

        saved_ids = BrandPrompt.objects.filter(website=website).values_list("prompt_id", flat=True)
        rejected_ids = RejectedBrandPrompt.objects.filter(website=website).values_list("prompt_id", flat=True)

        qs = (
            Prompt.objects.filter(is_active=True, industry=industry)
            .exclude(id__in=list(saved_ids))
            .exclude(id__in=list(rejected_ids))
        )

        suggestions = list(
            qs.select_related("industry")
            .order_by("-demand_score", "-created_at")[: self.LIMIT]
        )

        rows = []
        for p in suggestions:
            rows.append({
                "id": str(p.id),
                "text": p.text,
                "template_text": p.template_text or "",
                "topic": p.industry.name if p.industry_id else "",
                "intent_bucket": p.intent_bucket,
                "style": p.style,
                "demand_score": None,
                "source_score": p.demand_score,
                "suggested_at": p.created_at.isoformat() if p.created_at else None,
            })

        # Topics summary for the rail's 'Suggested Topics' section —
        # only include topics that actually have suggestions waiting.
        topic_counts = (
            qs.values_list("industry__name", flat=True)
        )
        from collections import Counter
        counter = Counter(t for t in topic_counts if t)
        topics = [{"name": n, "count": c} for n, c in counter.most_common()]

        return Response({
            "website_id": str(website.id),
            "rows": rows,
            "topics": topics,
            "total": len(rows),
        })


class WebsiteSuggestionActionView(TenantScopedAPIView):
    """
    Accept or reject a single suggested prompt.

    POST .../accept/{prompt_id}/  → creates a BrandPrompt row
    POST .../reject/{prompt_id}/  → creates a RejectedBrandPrompt row
    """

    def post(self, request, website_id, prompt_id, action):
        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id, is_active=True)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)
        if action == "accept":
            bp, _ = BrandPrompt.objects.get_or_create(website=website, prompt=prompt)
            # If the user had previously rejected this one, lift the
            # rejection so it doesn't keep being filtered out elsewhere.
            RejectedBrandPrompt.objects.filter(website=website, prompt=prompt).delete()
            return Response({"saved": True, "brand_prompt_id": str(bp.id)})
        if action == "reject":
            RejectedBrandPrompt.objects.get_or_create(website=website, prompt=prompt)
            return Response({"rejected": True})
        return Response({"detail": "action must be 'accept' or 'reject'."},
                        status=status.HTTP_400_BAD_REQUEST)


class WebsiteSuggestionBulkView(TenantScopedAPIView):
    """Accept or reject every prompt id passed in the body in one shot."""

    def post(self, request, website_id, action):
        website = self.get_website(website_id)
        ids = request.data.get("prompt_ids") or []
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "prompt_ids must be a non-empty array."},
                            status=status.HTTP_400_BAD_REQUEST)
        if action not in ("accept", "reject"):
            return Response({"detail": "action must be 'accept' or 'reject'."},
                            status=status.HTTP_400_BAD_REQUEST)

        prompts = list(Prompt.objects.filter(is_active=True, id__in=ids))
        if action == "accept":
            BrandPrompt.objects.bulk_create(
                [BrandPrompt(website=website, prompt=p) for p in prompts],
                ignore_conflicts=True,
            )
            RejectedBrandPrompt.objects.filter(website=website, prompt__in=prompts).delete()
            return Response({"accepted": len(prompts)})

        RejectedBrandPrompt.objects.bulk_create(
            [RejectedBrandPrompt(website=website, prompt=p) for p in prompts],
            ignore_conflicts=True,
        )
        return Response({"rejected": len(prompts)})


class PromptFanoutsView(TenantScopedAPIView):
    """List fan-out sub-queries the crawler captured for a saved prompt."""

    def get(self, request, website_id, prompt_id):
        from apps.prompt_library.models import PromptCrawlRun, PromptFanout

        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)

        fanouts = list(
            PromptFanout.objects
            .filter(website=website, prompt=prompt)
            .values("id", "text", "provider", "source", "created_at")
            .order_by("-created_at")[:50]
        )
        last_run = (
            PromptCrawlRun.objects
            .filter(website=website, prompt=prompt)
            .order_by("-created_at")
            .first()
        )

        return Response({
            "website_id": str(website.id),
            "prompt_id": str(prompt.id),
            "fanouts": [
                {**f, "id": str(f["id"]),
                 "created_at": f["created_at"].isoformat() if f["created_at"] else None}
                for f in fanouts
            ],
            "last_run": {
                "id": str(last_run.id),
                "status": last_run.status,
                "fanout_count": last_run.fanout_count,
                "source_count": last_run.source_count,
                "providers": last_run.providers,
                "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
                "completed_at": last_run.completed_at.isoformat() if last_run.completed_at else None,
                "error": _public_scan_error(last_run.error),
            } if last_run else None,
        })


class PromptCrawlTriggerView(TenantScopedAPIView):
    """Kick off a Celery task to crawl this prompt across providers."""

    def post(self, request, website_id, prompt_id):
        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)

        from apps.llm_ranking.services.audit_factory import (
            NoLLMProviderConfigured,
            configured_providers,
            missing_provider_keys,
        )

        if not configured_providers():
            return Response(
                NoLLMProviderConfigured(missing_provider_keys()).response_body(),
                status=status.HTTP_409_CONFLICT,
            )

        # mode=missing: the page's silent gap-fill (query only models that
        # never answered). Default: full re-run of every configured model.
        only_missing = (request.data or {}).get("mode") == "missing"

        try:
            from apps.prompt_library.tasks import crawl_prompt_for_website
            crawl_prompt_for_website.delay(
                str(website.id), str(prompt.id), only_missing,
                acting_user_id=str(request.user.id),
            )
            queued = True
        except Exception:
            # Celery worker not running — fall back to sync so the
            # button still does something useful in dev.
            from apps.prompt_library.services.prompt_crawler import crawl_prompt
            crawl_prompt(
                website, prompt, only_missing=only_missing,
                acting_user=request.user,
            )
            queued = False
        return Response({"queued": queued, "website_id": str(website.id),
                         "prompt_id": str(prompt.id)})


class PromptSourceScanView(TenantScopedAPIView):
    """Page-level sentiment for the domains an AI answer cited.

    POST — start a SourceScan seeded with this prompt's cited URLs (one
    representative URL per apex domain, most-cited first). The existing
    Source Intelligence pipeline fetches each page and scores how the
    brand is portrayed on it; the detail endpoint then surfaces the
    per-domain "on page" sentiment. Explicit-trigger only: this spends
    AI credits, so it never runs automatically.
    """

    MAX_SEEDS = 10  # matches the scan pipeline's per-scan result cap

    def post(self, request, website_id, prompt_id):
        from apps.citations.models import Citation, SourceScan, SourceScanStatus
        from apps.citations.tasks import run_source_scan

        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id)
        except Prompt.DoesNotExist:
            return Response({"detail": "Prompt not found."},
                            status=status.HTTP_404_NOT_FOUND)

        # One representative URL per apex domain, ranked by how often the
        # domain was cited, so the 10 seed slots cover 10 distinct domains.
        from collections import Counter, defaultdict
        cite_qs = (
            Citation.objects
            .filter(result__audit__website=website, result__source_prompt=prompt)
            .only("url", "apex_domain", "domain")
        )
        domain_counts: Counter = Counter()
        url_counts: dict[str, Counter] = defaultdict(Counter)
        for c in cite_qs:
            apex = c.apex_domain or c.domain
            if not apex or not c.url:
                continue
            domain_counts[apex] += 1
            url_counts[apex][c.url] += 1
        if not domain_counts:
            return Response(
                {"error": "no_citations",
                 "detail": "No cited sources for this prompt yet. Run a scan first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        seed_urls = [
            url_counts[apex].most_common(1)[0][0]
            for apex, _ in domain_counts.most_common(self.MAX_SEEDS)
        ]

        active = SourceScan.objects.filter(
            website=website,
            status__in=[SourceScanStatus.PENDING, SourceScanStatus.RUNNING],
        ).count()
        if active >= 3:
            return Response({"error": "too_many_active_scans"}, status=429)

        scan = SourceScan.objects.create(
            website=website,
            query=prompt.text[:500],
            seed_urls=seed_urls,
            source_prompt=prompt,
            created_by=request.user,
        )
        try:
            run_source_scan.delay(str(scan.id))
            queued = True
        except Exception:
            # No broker in dev -- run inline so the button still works.
            from apps.citations.services.source_scan import run_scan
            run_scan(scan)
            queued = False
        return Response(
            {"scan_id": str(scan.id), "status": scan.status,
             "seeded_domains": len(seed_urls), "queued": queued},
            status=status.HTTP_201_CREATED,
        )


class PromptScheduleView(TenantScopedAPIView):
    """Per-prompt run schedule.

    GET    — the schedule for this saved prompt (or ``{schedule: None}``).
    POST   — create/update it (cadence: daily / weekly / monthly).
    DELETE — remove it (the prompt reverts to "Not scheduled").

    ``prompt_id`` is the shared ``Prompt.id`` — the same identifier the
    crawl and detail endpoints use. The schedule is bound to the website's
    ``BrandPrompt`` for that prompt, so a prompt can only be scheduled once
    it has been saved to the website.
    """

    def _resolve_brand_prompt(self, request, website_id, prompt_id):
        """Return the website's BrandPrompt for this prompt, or None."""
        website = self.get_website(website_id)
        try:
            prompt = Prompt.objects.get(id=prompt_id)
        except Prompt.DoesNotExist:
            return None
        return BrandPrompt.objects.filter(website=website, prompt=prompt).first()

    def get(self, request, website_id, prompt_id):
        bp = self._resolve_brand_prompt(request, website_id, prompt_id)
        schedule = getattr(bp, "schedule", None) if bp is not None else None
        if schedule is None:
            return Response({"schedule": None})
        return Response({"schedule": PromptScheduleSerializer(schedule).data})

    def post(self, request, website_id, prompt_id):
        from datetime import timedelta

        from django.utils import timezone

        from apps.prompt_library.tasks import PROMPT_FREQUENCY_DELTAS

        bp = self._resolve_brand_prompt(request, website_id, prompt_id)
        if bp is None:
            return Response(
                {"detail": "Prompt is not saved to this website."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CreatePromptScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        delta = PROMPT_FREQUENCY_DELTAS.get(data["frequency"], timedelta(weeks=1))
        now = timezone.now()
        schedule, created = PromptSchedule.objects.update_or_create(
            brand_prompt=bp,
            defaults={
                "created_by": request.user,
                "is_enabled": data["is_enabled"],
                "frequency": data["frequency"],
                "next_run_at": now + delta,
                # Saving (or re-enabling) clears any auto-pause failure state.
                "consecutive_failures": 0,
                "last_failure_at": None,
            },
        )
        return Response(
            {"schedule": PromptScheduleSerializer(schedule).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, website_id, prompt_id):
        bp = self._resolve_brand_prompt(request, website_id, prompt_id)
        if bp is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        deleted, _ = PromptSchedule.objects.filter(brand_prompt=bp).delete()
        if not deleted:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Benchmark packs ──────────────────────────────────────────────────

def _serialize_pack(pack, *, include_claims: bool = False) -> dict:
    payload = {
        "id": str(pack.id),
        "source_kind": pack.source_kind,
        "label": pack.label,
        "url": pack.url,
        "fetched_title": pack.fetched_title,
        "fetched_summary": pack.fetched_summary,
        "status": pack.status,
        "extraction_error": pack.extraction_error,
        "created_at": pack.created_at.isoformat() if pack.created_at else None,
    }
    if include_claims:
        payload["claims"] = [
            {
                "id": str(c.id),
                "category": c.category,
                "text": c.text,
                "evidence": c.evidence,
                "ordinal": c.ordinal,
            }
            for c in pack.claims.all()
        ]
    return payload


class BenchmarkPackListCreateView(TenantScopedAPIView):
    """List a website's benchmark packs, or create a new one.

    POST body:
        {"source_kind": "url", "label": "...", "url": "https://..."}
        OR
        {"source_kind": "markdown", "label": "...", "markdown_content": "..."}

    Extraction runs inline (small material, ≤1 Claude call). Later we
    can push it into a Celery task if latency becomes a concern.
    """

    def get(self, request, website_id):
        from apps.prompt_library.models import BenchmarkPack

        self.get_website(website_id)
        packs = BenchmarkPack.objects.filter(website_id=website_id).prefetch_related("claims")
        return Response({"packs": [_serialize_pack(p, include_claims=True) for p in packs]})

    def post(self, request, website_id):
        from apps.prompt_library.models import BenchmarkPack
        from apps.prompt_library.services.benchmark_extractor import extract_pack

        website = self.get_website(website_id)

        source_kind = (request.data.get("source_kind") or "").strip().lower()
        if source_kind not in {BenchmarkPack.SOURCE_URL, BenchmarkPack.SOURCE_MARKDOWN}:
            return Response({"error": "source_kind must be 'url' or 'markdown'"}, status=400)

        label = (request.data.get("label") or "").strip()[:500]
        if source_kind == BenchmarkPack.SOURCE_URL:
            url = (request.data.get("url") or "").strip()[:2000]
            if not url or not url.lower().startswith(("http://", "https://")):
                return Response({"error": "url is required and must start with http(s)://"}, status=400)
            if not label:
                label = url
            pack = BenchmarkPack.objects.create(
                website=website, created_by=request.user,
                source_kind=source_kind, url=url, label=label,
            )
        else:
            content = (request.data.get("markdown_content") or "")[:100_000]
            if not content.strip():
                return Response({"error": "markdown_content is required"}, status=400)
            if not label:
                label = "Markdown pack"
            pack = BenchmarkPack.objects.create(
                website=website, created_by=request.user,
                source_kind=source_kind, label=label, markdown_content=content,
            )

        extract_pack(pack)  # inline; sets status → ready/failed
        pack.refresh_from_db()
        pack = type(pack).objects.prefetch_related("claims").get(pk=pack.pk)
        return Response(_serialize_pack(pack, include_claims=True), status=201)


class BenchmarkPackDetailView(TenantScopedAPIView):
    """Retrieve or delete a single benchmark pack."""

    def get(self, request, website_id, pack_id):
        from apps.prompt_library.models import BenchmarkPack

        self.get_website(website_id)
        pack = self.get_tenant_object(
            BenchmarkPack.objects.prefetch_related("claims"),
            pk=pack_id, website_id=website_id,
        )
        return Response(_serialize_pack(pack, include_claims=True))

    def delete(self, request, website_id, pack_id):
        from apps.prompt_library.models import BenchmarkPack

        self.get_website(website_id)
        pack = self.get_tenant_object(
            BenchmarkPack.objects.all(), pk=pack_id, website_id=website_id,
        )
        pack.delete()
        return Response(status=204)
