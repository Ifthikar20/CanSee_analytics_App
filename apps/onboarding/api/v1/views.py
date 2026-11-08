"""
Onboarding API.

Two endpoints, both authenticated:

    POST /api/v1/onboarding/scan/   — preview: scan + describe + competitors
    POST /api/v1/onboarding/save/   — commit: create the Website row

The scan endpoint is intentionally read-only so the user can iterate on
URL / description until they're happy before we persist anything.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.onboarding.api.v1.serializers import (
    OnboardingSaveSerializer,
    OnboardingScanSerializer,
)
from apps.onboarding.services.onboarding_service import (
    run_onboarding_scan,
    to_dict,
)
from core.resilience import TokenBucket


def _scan_bucket(user_id) -> TokenBucket:
    """Per-user throttle on /scan/. Burst 6, ~12 / minute steady state.
    A scan makes several Claude passes and up to 3 Google searches (those
    also have their own daily limit, ONBOARDING_SERP_DAILY_SEARCHES), so we
    don't want a frustrated user re-scanning every two seconds to drive
    cost up."""
    return TokenBucket(
        name=f"onboarding-scan:{user_id}",
        capacity=6,
        refill_per_second=0.2,
    )


class OnboardingScanView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _scan_bucket(request.user.id).try_acquire():
            return Response(
                {"error": "Slow down — too many scans. Try again in a moment."},
                status=429,
            )
        serializer = OnboardingScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        url = serializer.validated_data["url"]
        payload = run_onboarding_scan(url, user=request.user)
        return Response(to_dict(payload))


class OnboardingSaveView(APIView):
    """Persist the user's confirmed onboarding data into a Website row."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.websites.services.website_service import WebsiteService
        from core.validators.url_safety import assert_url_safe

        serializer = OnboardingSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Upsert: if the tenant already has a (live) website at this URL —
        # typical when they revisit onboarding — update it in place
        # rather than tripping the tenant-scoped unique constraint.
        validated_url = assert_url_safe(data["url"])
        website = WebsiteService.accessible_qs(request.user).filter(
            url=validated_url
        ).first()
        if website is None:
            # Same project cap the websites endpoint enforces — without
            # this, onboarding /save/ is a bypass around the plan limit.
            from apps.billing.services.plan_limits import projects_limit_for

            current_count = WebsiteService.accessible_qs(request.user).count()
            max_projects = projects_limit_for(request.user)
            if max_projects != -1 and current_count >= max_projects:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": "project_limit_reached",
                            "message": (
                                f"Your plan allows up to {max_projects} "
                                f"project{'s' if max_projects != 1 else ''}. "
                                "Upgrade to add more."
                            ),
                            "meta": {
                                "current": current_count,
                                "limit": max_projects,
                            },
                        },
                    },
                    status=403,
                )
            website = WebsiteService.create(
                user=request.user,
                url=data["url"],
                name=data["business_name"],
                industry=data.get("industry", "") or "",
            )
        else:
            website.name = data["business_name"] or website.name
            if data.get("industry"):
                website.industry = data["industry"]

        # Attach the user's edited description + keywords + competitors
        # straight onto the website row. These are the inputs the LLM
        # ranking pipeline reads on the first audit.
        if hasattr(website, "description"):
            website.description = (data.get("description") or "")[:600]
        if hasattr(website, "topics"):
            website.topics = data.get("keywords") or []
        if hasattr(website, "competitors"):
            website.competitors = data.get("competitors") or []
        website.save()

        return Response({
            "website_id": str(website.id),
            "name": website.name,
            "url": website.url,
        }, status=201)
