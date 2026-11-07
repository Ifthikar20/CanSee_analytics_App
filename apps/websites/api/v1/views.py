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


class WebsiteListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        websites = WebsiteService.accessible_qs(request.user).order_by("-created_at")
        serializer = WebsiteSerializer(websites, many=True)
        return Response(serializer.data)

    def post(self, request):
        from apps.billing.services.plan_limits import (
            current_plan_for,
            is_trialing_subscription,
            projects_limit_for,
        )

        # Count the whole tenant, not the individual: an org's project cap
        # is shared, otherwise every teammate gets their own allowance.
        current_count = WebsiteService.accessible_qs(request.user).count()
        max_projects = projects_limit_for(request.user)
        if max_projects != -1 and current_count >= max_projects:
            sub = getattr(request.user, "subscription", None)
            if is_trialing_subscription(sub):
                message = (
                    f"Your free trial includes {max_projects} project"
                    f"{'s' if max_projects != 1 else ''}. The full Pro "
                    "allowance unlocks when your subscription starts."
                )
            else:
                plan_name = str(current_plan_for(request.user)).capitalize()
                message = (
                    f"Your {plan_name} plan allows up to {max_projects} "
                    f"project{'s' if max_projects != 1 else ''}. "
                    "Upgrade to add more."
                )
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
