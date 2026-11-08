from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.websites.api.v1.serializers import (
    WebsiteCreateSerializer,
    WebsiteSerializer,
    WebsiteSettingsSerializer,
    WebsiteUpdateSerializer,
)
from apps.websites.services.pixel_service import PixelService
from apps.websites.services.verification_service import VerificationService
from apps.websites.services.website_service import WebsiteService
from core.views import TenantScopedAPIView


def _projects_limit(user) -> int:
    from apps.billing.services.plan_limits import projects_limit_for

    return projects_limit_for(user)


def project_limit_message(user, max_projects: int) -> str:
    """The 403 text when the account is at its project cap, naming the plan
    the cap comes from (PLAN_NAMES) and what adds room."""
    from apps.billing.services.plan_limits import (
        _resolve_plan_key,
        is_trialing_subscription,
        tier_for_subscription,
    )
    from core.utils.constants import PLAN_LIMITS, PLAN_NAMES, Plan

    projects = f"{max_projects} project{'s' if max_projects != 1 else ''}"
    sub = getattr(user, "subscription", None)
    if is_trialing_subscription(sub):
        tier = tier_for_subscription(sub)
        full = PLAN_LIMITS.get(tier, {}).get("projects", max_projects)
        unlocks = (
            f"The {PLAN_NAMES.get(tier, 'paid')} plan's {full} projects unlock "
            if isinstance(full, int) and full > max_projects
            else "More projects unlock "
        )
        return f"Your free trial includes {projects}. {unlocks}when your subscription starts."
    plan = _resolve_plan_key(user)
    name = PLAN_NAMES.get(plan, PLAN_NAMES[Plan.FREE])
    if plan == Plan.FREE:
        nxt = "Choose the 100 or 200 prompt plan to add more."
    else:
        nxt = "Delete a project to add another, or contact us about the Business plan."
    return f"Your {name} plan allows up to {projects}. {nxt}"


class WebsiteListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        websites = WebsiteService.accessible_qs(request.user).order_by("-created_at")
        serializer = WebsiteSerializer(websites, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Count the whole tenant, not the individual: an org's project cap
        # is shared, otherwise every teammate gets their own allowance.
        current_count = WebsiteService.accessible_qs(request.user).count()
        max_projects = _projects_limit(request.user)
        if max_projects != -1 and current_count >= max_projects:
            message = project_limit_message(request.user, max_projects)
            # Error responses bypass the EnvelopeRenderer, so this must
            # carry the envelope shape the frontend reads
            # (error.message drives the toast).
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "project_limit_reached",
                        "message": message,
                        "meta": {"current": current_count, "limit": max_projects},
                    },
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = WebsiteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        website = WebsiteService.create(user=request.user, **serializer.validated_data)
        return Response(WebsiteSerializer(website).data, status=status.HTTP_201_CREATED)


class WebsiteDetailView(TenantScopedAPIView):
    website_url_kwarg = "pk"

    def _get_website(self, request, pk):
        return self.get_website(pk)

    def get(self, request, pk):
        website = self._get_website(request, pk)
        return Response(WebsiteSerializer(website).data)

    def put(self, request, pk):
        website = self._get_website(request, pk)
        serializer = WebsiteUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        website = WebsiteService.update(website=website, user=request.user, **serializer.validated_data)
        return Response(WebsiteSerializer(website).data)

    patch = put

    def delete(self, request, pk):
        website = self._get_website(request, pk)
        WebsiteService.delete(website=website, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PixelView(TenantScopedAPIView):
    website_url_kwarg = "pk"

    def get(self, request, pk):
        website = self.get_website(pk)
        snippet = PixelService.get_snippet(website=website)
        return Response({"pixel_key": str(website.pixel_key), "snippet": snippet})


class PixelVerifyView(TenantScopedAPIView):
    website_url_kwarg = "pk"

    def post(self, request, pk):
        website = self.get_website(pk)
        result = VerificationService.verify_pixel(website=website)
        return Response(result)


class PixelRegenerateView(TenantScopedAPIView):
    website_url_kwarg = "pk"

    def post(self, request, pk):
        website = self.get_website(pk)
        website = WebsiteService.regenerate_pixel_key(website=website, user=request.user)
        return Response({"pixel_key": str(website.pixel_key)})


class WebsiteSettingsView(TenantScopedAPIView):
    website_url_kwarg = "pk"

    def get(self, request, pk):
        website = self.get_website(pk)
        serializer = WebsiteSettingsSerializer(website.settings)
        return Response(serializer.data)

    def put(self, request, pk):
        website = self.get_website(pk)
        serializer = WebsiteSettingsSerializer(website.settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
