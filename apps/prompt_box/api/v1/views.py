"""Ask AI API (wave 4 contract 4), under /api/v1/prompt-box/websites/<wid>/:

POST  estimate/              what a question costs at most; no side effects
GET   runs/                  the project's 20 newest runs (summaries)
POST  runs/                  ask a confirmed question (201)
GET   runs/<rid>/            one run; ?full=1 adds each answer's text, citation
                             URLs and search queries
POST  runs/<rid>/cancel/     stop a run (idempotent)

Viewers read; members ask and stop; others get 404. Spend goes to the
caller, as insights runs. Every view that shows runs first interrupts runs
that stopped reporting.
"""

from rest_framework import status
from rest_framework.response import Response

from apps.prompt_box.models import PromptBoxRun
from apps.prompt_box.services import runs
from core.exceptions import CanseeException, ResourceNotFound
from core.views.base import TenantScopedAPIView


class PromptBoxView(TenantScopedAPIView):
    def get_website(self, website_id=None, min_role: str = "member"):
        from apps.websites.services.website_service import WebsiteService

        if website_id is None:
            website_id = self.kwargs.get(self.website_url_kwarg)
        return WebsiteService.get_for_user(
            user=self.request.user, website_id=website_id, min_role=min_role,
        )


def _body(request) -> dict:
    data = request.data
    if not isinstance(data, dict):
        message = runs.TEXT_MESSAGE
        raise CanseeException(
            message, code="invalid", status_code=400,
            details={"field": "text", "message": message},
        )
    return data


def _get_run(website, run_id) -> PromptBoxRun:
    run = PromptBoxRun.objects.filter(website=website, id=run_id).first()
    if run is None:
        raise ResourceNotFound("Run not found.")
    return run


class EstimateView(PromptBoxView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        body = _body(request)
        runs.sweep_stale(website)
        return Response(runs.estimate(website, request.user, body))


class RunsView(PromptBoxView):
    def get(self, request, website_id):
        website = self.get_website(website_id, min_role="viewer")
        runs.sweep_stale(website)
        return Response(runs.runs_list(website))

    def post(self, request, website_id):
        website = self.get_website(website_id)
        body = _body(request)
        runs.sweep_stale(website)
        run = runs.create_run(website, request.user, body, body.get("confirm"))
        return Response(runs.run_payload(run), status=status.HTTP_201_CREATED)


class RunView(PromptBoxView):
    def get(self, request, website_id, run_id):
        website = self.get_website(website_id, min_role="viewer")
        runs.sweep_stale(website)
        full = (request.query_params.get("full") or "").strip().lower() in ("1", "true")
        return Response(runs.run_payload(_get_run(website, run_id), full=full))


class RunCancelView(PromptBoxView):
    def post(self, request, website_id, run_id):
        website = self.get_website(website_id)
        run = runs.cancel_run(_get_run(website, run_id))
        return Response(runs.run_payload(run))
