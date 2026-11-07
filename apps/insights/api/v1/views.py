"""Insights API (wave 2 contract 1e and sections 3-9).

Every path is under /api/v1/insights/websites/<wid>/:

GET   runs/options/?kind=            what a run of this kind can do, and its cost basis
POST  runs/estimate/                 the estimate for a kind's params; no side effects
GET   runs/?kind=                    the 20 newest runs
POST  runs/                          start a confirmed run (202)
GET   runs/<rid>/                    one run
POST  runs/<rid>/cancel/             stop a run (idempotent)
GET   sentiment/  fact-check/  competitors/  citations/  crawler-access/
      pages/  prompt-ideas/  fan-outs/          the analytics payloads
PATCH prompt-ideas/<iid>/            dismiss or restore an idea
POST  prompt-ideas/<iid>/track/      track an idea and ask the engines (201)
GET   products/   POST products/     tracked products / add one (201)
GET   products/<pid>/  PATCH  DELETE (archive, 204)

Views are tenant scoped (member or above; others get 404). With
INSIGHTS_ENABLED off every endpoint answers 503 ``insights_disabled``.
Every view that shows runs first interrupts runs that stopped reporting.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.insights.models import InsightRun, PromptIdea, TrackedProduct
from apps.insights.services import runs
from apps.llm_ranking.services import answer_window
from core.exceptions import CanseeException, ResourceNotFound
from core.views.base import TenantScopedAPIView

VERDICTS = ("supported", "contradicted", "not_verifiable", "unchecked")
TOPICS = (
    "pricing", "features", "integrations", "company", "availability", "support", "security",
    "performance", "other",
)
PAGE_FILTERS = (
    "all", "cited", "retrieved_not_cited", "ranking", "ai_visits", "crawled", "failing_checks",
)
IDEA_SOURCES = ("paa", "related_search", "fanout", "ai_suggestion", "sub_query")
IDEA_STATUSES = ("new", "tracked", "dismissed")


class _Envelope(JSONRenderer):
    """The success envelope for every insights payload, none of which is a
    paginated list (EnvelopeRenderer reads a top-level ``results`` key as one)."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context.get("response") if renderer_context else None
        if response is not None and response.status_code == 204:
            return b""
        if response is not None and response.status_code < 400:
            data = {"success": True, "data": data}
        return super().render(data, accepted_media_type, renderer_context)


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


class InsightsView(TenantScopedAPIView):
    renderer_classes = [_Envelope]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.INSIGHTS_ENABLED:
            raise CanseeException(
                "Insights are turned off on this server.", code="insights_disabled", status_code=503,
            )


def _query(request, name: str) -> str | None:
    return (request.query_params.get(name) or "").strip() or None


def _filters(request, *, interval: bool = True, engine: bool = True, prompt: bool = True) -> tuple:
    """(period, interval, engine, prompt uuid) of the query (wave 1 rules)."""
    return answer_window.parse_filters(
        period=_query(request, "period"),
        interval=_query(request, "interval") if interval else None,
        engine=_query(request, "engine") if engine else None,
        prompt_id=_query(request, "prompt_id") if prompt else None,
    )


def _choice(request, name: str, allowed: tuple, default=None):
    value = _query(request, name)
    if value is None:
        return default
    if value not in allowed:
        raise _invalid(name, f"Choose {name} from {', '.join(allowed)}.")
    return value


def _tracked_prompt(website, prompt_uuid) -> None:
    """404 for a prompt id that is not a tracked prompt of this project."""
    from apps.prompt_library.models import BrandPrompt

    if prompt_uuid is None:
        return
    if not BrandPrompt.objects.filter(
        website=website, prompt_id=prompt_uuid, is_archived=False,
    ).exists():
        raise ResourceNotFound("Prompt not found.")


def _body(request) -> dict:
    data = request.data
    if not isinstance(data, dict):
        raise _invalid("body", "Send a JSON object.")
    return data


# -- runs --------------------------------------------------------------------


def _get_run(website, run_id) -> InsightRun:
    run = (
        InsightRun.objects.select_related("created_by")
        .filter(website=website, id=run_id).first()
    )
    if run is None:
        raise ResourceNotFound("Run not found.")
    return run


class RunOptionsView(InsightsView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        runs.sweep_stale(website)
        return Response(runs.options(website, request.user, _query(request, "kind")))


class RunEstimateView(InsightsView):
    def post(self, request, website_id):
        website = self.get_website(website_id)
        body = _body(request)
        runs.sweep_stale(website)
        return Response(runs.estimate(website, request.user, body.get("kind"), body.get("params")))


class RunsView(InsightsView):
    def get(self, request, website_id):
        website = self.get_website(website_id)
        kind = _query(request, "kind")
        if kind is not None:
            runs.get_kind(kind)
        runs.sweep_stale(website)
        return Response(runs.runs_list(website, kind))

    def post(self, request, website_id):
        website = self.get_website(website_id)
        body = _body(request)
        runs.sweep_stale(website)
        run = runs.create_run(
            website, request.user, body.get("kind"), body.get("params"), body.get("confirm"),
        )
        return Response(runs.run_payload(run), status=status.HTTP_202_ACCEPTED)


class RunView(InsightsView):
    def get(self, request, website_id, run_id):
        website = self.get_website(website_id)
        runs.sweep_stale(website)
        return Response(runs.run_payload(_get_run(website, run_id)))


class RunCancelView(InsightsView):
    def post(self, request, website_id, run_id):
        website = self.get_website(website_id)
        run = runs.cancel_run(_get_run(website, run_id))
        return Response(runs.run_payload(run))


# -- answer analysis, competitors, citations -------------------------------------


class SentimentView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import sentiment

        website = self.get_website(website_id)
        period, interval, engine, prompt_uuid = _filters(request)
        runs.sweep_stale(website)
        return Response(sentiment.build(
            website, period=period, interval=interval, engine=engine, prompt_id=prompt_uuid,
        ))


class FactCheckView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import fact_check

        website = self.get_website(website_id)
        period, interval, engine, prompt_uuid = _filters(request)
        verdict = _choice(request, "verdict", VERDICTS)
        topic = _choice(request, "topic", TOPICS)
        runs.sweep_stale(website)
        return Response(fact_check.build(
            website, period=period, interval=interval, engine=engine, prompt_id=prompt_uuid,
            verdict=verdict, topic=topic,
        ))


class CompetitorsView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import competitors

        website = self.get_website(website_id)
        period, interval, engine, _prompt = _filters(request, prompt=False)
        return Response(competitors.build(
            website, period=period, interval=interval, engine=engine,
        ))


class CitationsView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import citation_analytics

        website = self.get_website(website_id)
        period, _interval, engine, _prompt = _filters(request, interval=False, prompt=False)
        return Response(citation_analytics.build(website, period=period, engine=engine))


# -- crawler access, pages, fan-outs (services by BE-site) -----------------------


class CrawlerAccessView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import site_audit

        website = self.get_website(website_id)
        runs.sweep_stale(website)
        return Response(site_audit.crawler_access(website))


class PagesView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import pages

        website = self.get_website(website_id)
        period, _interval, _engine, _prompt = _filters(
            request, interval=False, engine=False, prompt=False,
        )
        page_filter = _choice(request, "filter", PAGE_FILTERS, default="all")
        return Response(pages.build(website, period=period, filter=page_filter))


class FanoutsView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import fanouts

        website = self.get_website(website_id)
        period, _interval, engine, prompt_uuid = _filters(request, interval=False)
        _tracked_prompt(website, prompt_uuid)
        runs.sweep_stale(website)
        return Response(fanouts.build(
            website, period=period, engine=engine,
            prompt_id=str(prompt_uuid) if prompt_uuid else None,
        ))


# -- prompt research -------------------------------------------------------------


def _get_idea(website, idea_id) -> PromptIdea:
    idea = PromptIdea.objects.filter(website=website, id=idea_id).first()
    if idea is None:
        raise ResourceNotFound("Idea not found.")
    return idea


class PromptIdeasView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import prompt_ideas

        website = self.get_website(website_id)
        source = _choice(request, "source", IDEA_SOURCES)
        idea_status = _choice(request, "status", IDEA_STATUSES, default="new")
        runs.sweep_stale(website)
        return Response(prompt_ideas.build(
            website, source=source, status=idea_status, user=request.user,
        ))


class PromptIdeaView(InsightsView):
    def patch(self, request, website_id, idea_id):
        from apps.insights.services import prompt_ideas

        website = self.get_website(website_id)
        body = _body(request)
        value = body.get("status")
        if value not in ("dismissed", "new"):
            raise _invalid("status", "Choose status dismissed or new.")
        idea = prompt_ideas.set_status(website, _get_idea(website, idea_id), value)
        return Response(idea)


class PromptIdeaTrackView(InsightsView):
    def post(self, request, website_id, idea_id):
        from apps.insights.services import prompt_ideas

        website = self.get_website(website_id)
        body = _body(request)
        idea = _get_idea(website, idea_id)
        out = prompt_ideas.track(website, request.user, idea, body.get("confirm"))
        return Response(out, status=status.HTTP_201_CREATED)


# -- products (services by BE-site) ----------------------------------------------


def _get_product(website, product_id) -> TrackedProduct:
    product = TrackedProduct.objects.filter(
        website=website, id=product_id, is_archived=False,
    ).first()
    if product is None:
        raise ResourceNotFound("Product not found.")
    return product


def _product_out(value) -> dict:
    """The Product payload of a create or update (the service returns the
    row or its payload)."""
    from apps.insights.services import shopping

    return value if isinstance(value, dict) else shopping.product_detail(value)


class ProductsView(InsightsView):
    def get(self, request, website_id):
        from apps.insights.services import shopping

        website = self.get_website(website_id)
        runs.sweep_stale(website)
        return Response(shopping.products_payload(website))

    def post(self, request, website_id):
        from apps.insights.services import shopping

        website = self.get_website(website_id)
        product = shopping.create_product(website, request.user, _body(request))
        return Response(_product_out(product), status=status.HTTP_201_CREATED)


class ProductView(InsightsView):
    def get(self, request, website_id, product_id):
        from apps.insights.services import shopping

        website = self.get_website(website_id)
        return Response(shopping.product_detail(_get_product(website, product_id)))

    def patch(self, request, website_id, product_id):
        from apps.insights.services import shopping

        website = self.get_website(website_id)
        product = shopping.update_product(_get_product(website, product_id), _body(request))
        return Response(_product_out(product))

    def delete(self, request, website_id, product_id):
        from apps.insights.services import shopping

        website = self.get_website(website_id)
        shopping.archive_product(_get_product(website, product_id))
        return Response(status=status.HTTP_204_NO_CONTENT)
