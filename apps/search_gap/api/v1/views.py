"""Search vs AI gap API.

GET  websites/<wid>/overview/                 every tracked prompt, Google vs each engine
GET  websites/<wid>/prompts/<pid>/            one prompt in full, with where-to-change advice
POST websites/<wid>/prompts/<pid>/check/      run (or reuse) the Google rank check

``pid`` is the library Prompt id, the same id the prompt-library endpoints
use; the website scopes it to the tenant's BrandPrompt.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response

from apps.citations.services import serp_google
from apps.prompt_library.models import BrandPrompt
from apps.search_gap.services.overview import build_overview, build_prompt_detail
from core.views.base import TenantScopedAPIView


class _BrandPromptMixin:
    def get_brand_prompt(self, website, prompt_id):
        return get_object_or_404(
            BrandPrompt.objects.select_related("prompt", "website"),
            website=website, prompt_id=prompt_id,
        )


class SearchGapOverviewView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(build_overview(website))


class SearchGapPromptView(_BrandPromptMixin, TenantScopedAPIView):
    def get(self, request, website_id, prompt_id):
        website = self.get_website(website_id)
        brand_prompt = self.get_brand_prompt(website, prompt_id)
        return Response(build_prompt_detail(website, brand_prompt))


class SearchGapCheckView(_BrandPromptMixin, TenantScopedAPIView):
    """Queue a Google rank check for one prompt.

    The AI side is refreshed through the existing prompt-library crawl
    endpoint, which already owns the spend-cap and in-flight guards.
    """

    def post(self, request, website_id, prompt_id):
        from apps.search_gap.tasks import check_google_rank

        website = self.get_website(website_id)
        brand_prompt = self.get_brand_prompt(website, prompt_id)
        if not serp_google.is_configured():
            return Response(
                {"error": "serpapi_not_configured"},
                status=status.HTTP_409_CONFLICT,
            )
        force = bool(request.data.get("force"))
        check_google_rank.delay(str(brand_prompt.id), force=force)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)
