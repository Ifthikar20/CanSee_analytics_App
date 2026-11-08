"""Rankings by country API (contract section 2c).

GET  websites/<wid>/countries/                       the country matrix (?prompt_id=&engine=)
GET  websites/<wid>/countries/options/               what can be checked, and at what cost
POST websites/<wid>/countries/estimate/              the estimate for a selection; no side effects
GET  websites/<wid>/countries/runs/                  the 20 newest checks
POST websites/<wid>/countries/runs/                  start a confirmed check (202)
GET  websites/<wid>/countries/runs/<rid>/            one check with its cells
POST websites/<wid>/countries/runs/<rid>/cancel/     stop a check (idempotent)
GET  websites/<wid>/countries/results/<cid>/         one cell in full

Every read of the runs first interrupts checks that stopped reporting.
"""

import uuid

from django.conf import settings
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.search_gap.models import CountryCheckResult, CountryCheckRun
from apps.search_gap.services import country_checks, country_geo, country_matrix
from core.exceptions import CanseeException, ResourceNotFound
from core.views.base import TenantScopedAPIView


class _RunEnvelope(JSONRenderer):
    """The success envelope for a payload with its own ``results`` key,
    which EnvelopeRenderer would take for a paginated list."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context.get("response") if renderer_context else None
        if response is not None and response.status_code < 400:
            data = {"success": True, "data": data}
        return super().render(data, accepted_media_type, renderer_context)


def _require_enabled() -> None:
    if not settings.COUNTRY_CHECKS_ENABLED:
        raise CanseeException(
            "Country checks are turned off on this server.",
            code="country_checks_disabled", status_code=503,
        )


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def _get_run(website, run_id) -> CountryCheckRun:
    run = (
        CountryCheckRun.objects.select_related("created_by")
        .filter(website=website, id=run_id).first()
    )
    if run is None:
        raise ResourceNotFound("Country check not found.")
    return run


class CountryOptionsView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        country_checks.sweep_stale(website)
        return Response(country_matrix.options_payload(website, request.user))


class CountryEstimateView(TenantScopedAPIView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        _require_enabled()
        country_checks.sweep_stale(website)
        plan = country_checks.validate_request(website, request.data)
        return Response(country_checks.estimate(website, request.user, plan))


class CountryRunsView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        country_checks.sweep_stale(website)
        return Response(country_matrix.runs_list(website))

    def post(self, request, website_id):
        website = self.get_website(website_id)
        _require_enabled()
        country_checks.sweep_stale(website)
        plan = country_checks.validate_request(website, request.data)
        confirm = country_checks.parse_confirm(request.data)
        run = country_checks.create_run(website, request.user, plan, confirm)
        return Response(country_matrix.run_payload(run), status=status.HTTP_202_ACCEPTED)


class CountryRunView(TenantScopedAPIView):
    renderer_classes = [_RunEnvelope]

    def get(self, request, website_id, run_id):
        website = self.get_website(website_id)
        country_checks.sweep_stale(website)
        run = _get_run(website, run_id)
        return Response(country_matrix.run_payload(run, with_results=True))


class CountryRunCancelView(TenantScopedAPIView):
    def post(self, request, website_id, run_id):
        website = self.get_website(website_id)
        _require_enabled()
        run = country_checks.cancel_run(_get_run(website, run_id))
        return Response(country_matrix.run_payload(run))


class CountryMatrixView(TenantScopedAPIView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        engine = (request.query_params.get("engine") or "").strip().lower() or None
        if engine is not None and engine not in (country_geo.GOOGLE, *country_geo.ENGINE_KEYS):
            raise _invalid("engine", "Choose google or an AI engine.")
        prompt_id = (request.query_params.get("prompt_id") or "").strip() or None
        if prompt_id is not None:
            try:
                prompt_id = str(uuid.UUID(prompt_id))
            except ValueError:
                raise _invalid("prompt_id", "Choose a tracked prompt.") from None
        country_checks.sweep_stale(website)
        return Response(country_matrix.build_matrix(website, prompt_id=prompt_id, engine=engine))


class CountryResultView(TenantScopedAPIView):
    def get(self, request, website_id, result_id):
        website = self.get_website(website_id)
        _require_enabled()
        result = (
            CountryCheckResult.objects.select_related("brand_prompt__prompt")
            .filter(website=website, id=result_id).first()
        )
        if result is None:
            raise ResourceNotFound("Country check result not found.")
        return Response(country_matrix.cell_detail_payload(result))
