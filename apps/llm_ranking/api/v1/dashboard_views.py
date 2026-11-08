"""Visibility dashboard API (pages contract section 5).

GET <website_id>/visibility-dashboard/?period=&interval=&engine=&prompt_id=

Visibility, share of voice, sentiment and citations over time, by engine
and by prompt. ``prompt_id`` is the library Prompt id; the website scopes
it to the tenant's tracked prompts.
"""

from rest_framework.response import Response

from apps.llm_ranking.services.visibility_dashboard import build_dashboard
from core.views import TenantScopedAPIView


class VisibilityDashboardView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        params = request.query_params
        return Response(build_dashboard(
            website,
            period=params.get("period") or "30d",
            interval=params.get("interval") or None,
            engine=params.get("engine") or None,
            prompt_id=params.get("prompt_id") or None,
        ))
