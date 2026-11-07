"""Autopilot API (wave 4 contract 3), under /api/v1/autopilot/:

GET   websites/<wid>/                       settings payload (3a)
PUT   websites/<wid>/                       save settings; answers the payload
POST  websites/<wid>/run/estimate/          Run now estimate, no side effects
POST  websites/<wid>/run/                   Run now with {"confirm": {...}} (201)
GET   websites/<wid>/runs/                  the last 30 RunSummary rows
GET   websites/<wid>/runs/<rid>/            RunSummary, its prompt rows and its text
POST  websites/<wid>/runs/<rid>/cancel/     stop a run (no text is sent for it)
POST  websites/<wid>/text/test/             template G to the project's number
GET   websites/<wid>/ranking/               the prompt ranking (3c)
GET   links/<code>/                         where the text's link points

Viewers read; members write and run. Anyone else gets 404, signed out 401.
The views that show runs first end runs that stopped reporting.
"""

from rest_framework import status
from rest_framework.response import Response

from apps.autopilot.models import AutopilotRun
from apps.autopilot.services import pipeline, ranking, text
from apps.autopilot.services import settings as autopilot_settings
from core.exceptions import ResourceNotFound
from core.views.base import TenantScopedAPIView

RUNS_LIMIT = 30


class AutopilotView(TenantScopedAPIView):
    """Reads need viewer, everything else member (contract 3)."""

    def get_website(self, website_id=None, *, min_role: str | None = None):
        from apps.websites.services.website_service import WebsiteService

        if website_id is None:
            website_id = self.kwargs.get(self.website_url_kwarg)
        if min_role is None:
            min_role = "viewer" if self.request.method == "GET" else "member"
        return WebsiteService.get_for_user(
            user=self.request.user, website_id=website_id, min_role=min_role,
        )


def _get_run(website, run_id) -> AutopilotRun:
    run = (
        AutopilotRun.objects.select_related("analysis_run")
        .filter(website=website, id=run_id).first()
    )
    if run is None:
        raise ResourceNotFound("Run not found.")
    return run


def _client_ip(request) -> str | None:
    return request.META.get("REMOTE_ADDR", "") or None


class SettingsView(AutopilotView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        pipeline.sweep_stale(website)
        return Response(autopilot_settings.payload(website, request.user))

    def put(self, request, website_id):
        website = self.get_website(website_id)
        autopilot_settings.save(website, request.user, request.data, ip=_client_ip(request))
        return Response(autopilot_settings.payload(website, request.user))


class RunEstimateView(AutopilotView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        return Response(pipeline.run_estimate(website, request.user))


class RunCreateView(AutopilotView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        body = request.data if isinstance(request.data, dict) else {}
        run = pipeline.create_manual_run(website, request.user, body.get("confirm"))
        run = _get_run(website, run.id)
        return Response(pipeline.run_summary(run), status=status.HTTP_201_CREATED)


class RunsView(AutopilotView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        pipeline.sweep_stale(website)
        runs = (
            AutopilotRun.objects.filter(website=website).select_related("analysis_run")
            .order_by("-created_at")[:RUNS_LIMIT]
        )
        return Response([pipeline.run_summary(run) for run in runs])


class RunView(AutopilotView):
    def get(self, request, website_id, run_id):
        website = self.get_website(website_id)
        pipeline.sweep_stale(website)
        return Response(pipeline.run_detail(_get_run(website, run_id)))


class RunCancelView(AutopilotView):
    def post(self, request, website_id, run_id):
        website = self.get_website(website_id)
        run = pipeline.cancel_run(_get_run(website, run_id))
        return Response(pipeline.run_summary(_get_run(website, run.id)))


class TextTestView(AutopilotView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        return Response(text.send_test(website))


class RankingView(AutopilotView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(ranking.rank_prompts(website))


class LinkView(TenantScopedAPIView):
    """Where the text's link points; 404 unless the caller can open the project."""

    def get(self, request, code):
        from apps.websites.services.website_service import WebsiteService

        run = AutopilotRun.objects.filter(link_code=str(code)[:12]).first()
        if run is None:
            raise ResourceNotFound("Link not found.")
        try:
            WebsiteService.get_for_user(
                user=request.user, website_id=run.website_id, min_role="viewer",
            )
        except ResourceNotFound:
            raise ResourceNotFound("Link not found.") from None
        return Response({
            "website_id": str(run.website_id),
            "run_id": str(run.id),
            "local_date": run.local_date.isoformat(),
        })
