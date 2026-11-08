"""Agent workspace API served by gunicorn, plus the browse endpoint the
agent process serves under live/ (design/agent_contract.md section 1).

Access rules:
    * Every endpoint needs a signed-in user; every endpoint but status/
      answers 503 agent_disabled while AGENT_ENABLED is off.
    * Live endpoints answer 503 agent_unavailable outside the agent
      process (AGENT_PROCESS), so a gunicorn worker never holds a slow
      fetch.
    * The website resolves through WebsiteService.get_for_user. Viewers
      may read; anything that changes data needs member or above.
    * Chats and messages are private: they also filter on the user.
      Artifacts, their reviews, reports and the context are shared by the
      website's members (context_views, review_views).
    * The research agents' swarms are private to the chat's user (another
      member gets 404); findings are shared by the website's members
      (design/swarm_contract.md section 6).
"""
from __future__ import annotations

from django.db.models import prefetch_related_objects
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent.api.v1.serializers import (
    ArtifactSummarySerializer,
    ArtifactUpdateSerializer,
    ArtifactVersionSerializer,
    ArtifactVersionSummarySerializer,
    BrowseSerializer,
    ChatCreateSerializer,
    ChatSummarySerializer,
    ChatUpdateSerializer,
    FeedbackSerializer,
    FindingCreateSerializer,
    FindingListParamsSerializer,
    FindingSerializer,
    FindingUpdateSerializer,
    ReportCreateSerializer,
    artifact_detail,
    chat_detail,
    swarm_detail,
    swarm_version,
)
from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.constants import ArtifactKind
from apps.agent.exceptions import AgentDisabled, AgentNotConfigured, AgentUnavailable
from apps.agent.services import (
    activity,
    artifacts,
    browse,
    catalog,
    chats,
    findings,
    reports,
    suggestions,
    swarms,
)
from apps.agent.services.access import can_write
from core.exceptions import CanseeException, PermissionDenied, RateLimited
from core.interceptors.response_envelope import EnvelopeRenderer
from core.quota import DailyQuota
from core.resilience import TokenBucket
from core.views.base import TenantScopedAPIView

browse_quota = DailyQuota(
    namespace="agent_browse", setting_name="AGENT_BROWSE_DAILY_LIMIT", default_limit=300,
)
VIEWER_MESSAGE = "Viewers can see this project's agent work but not change it."


class InvalidRequest(CanseeException):
    """400 ``invalid`` with the field errors in ``details.fields``."""

    def __init__(self, errors):
        super().__init__(
            _first_error(errors) or "Please check your input and try again.",
            code="invalid",
            status_code=400,
            details={"fields": errors},
        )


class BrowseLimitReached(CanseeException):
    def __init__(self, limit: int):
        super().__init__(
            f"You have opened {limit} pages today, the daily limit. "
            "It resets at midnight UTC.",
            code="browse_limit_reached",
            status_code=429,
        )


class PlainEnvelopeRenderer(EnvelopeRenderer):
    """Wraps ``data`` as it is. The chat list's ``{results, next_cursor}`` is
    the contract's data, not a paginated list for EnvelopeRenderer to unpack."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = (renderer_context or {}).get("response")
        if response is not None and (response.status_code == 204 or response.status_code >= 400):
            return super().render(data, accepted_media_type, renderer_context)
        return JSONRenderer.render(
            self, {"success": True, "data": data}, accepted_media_type, renderer_context,
        )


class AgentAPIView(TenantScopedAPIView):
    """Base of every gated agent endpoint."""

    # True for the live/ routes the agent process serves.
    live = False
    # Request and response shapes are design/agent_contract.md, built by
    # hand; a generated OpenAPI schema cannot describe them, so the agent
    # endpoints stay out of it.
    schema = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        conf = agent_conf()
        if not conf.enabled:
            raise AgentDisabled()
        if self.live and not conf.process:
            raise AgentUnavailable()

    def website(self, *, write: bool = False):
        """The URL's website; viewers pass for reads, writes need member+."""
        from apps.websites.services.website_service import WebsiteService

        user = self.request.user
        website = WebsiteService.get_for_user(
            user=user, website_id=self.kwargs["website_id"], min_role="viewer",
        )
        if write and not can_write(user, website):
            raise PermissionDenied(VIEWER_MESSAGE)
        return website


# ── Status and catalog ────────────────────────────────────────────────────


class AgentStatusView(APIView):
    """Whether the agent can run, and what is configured. Never gated.

    ``?website_id`` reads the prompt runs of that project's owner (a 404
    for a project the user cannot see); ``?runs_needed`` is how many prompt
    runs the caller is about to use (setup issue ``prompt_runs_low``)."""

    permission_classes = [IsAuthenticated]
    schema = None

    def get(self, request):
        from apps.websites.services.website_service import WebsiteService

        website = None
        website_id = request.query_params.get("website_id")
        if website_id:
            website = WebsiteService.get_for_user(
                user=request.user, website_id=website_id, min_role="viewer",
            )
        needed = request.query_params.get("runs_needed")
        try:
            runs_needed = max(0, int(needed)) if needed not in (None, "") else None
        except ValueError:
            raise CanseeException("runs_needed must be a whole number.", code="invalid",
                                  status_code=400) from None
        return Response(catalog.status_for(request.user, website, runs_needed))


class AgentCatalogView(AgentAPIView):
    def get(self, request):
        return Response(catalog.agents())


# ── Chats and messages ────────────────────────────────────────────────────


class ChatListCreateView(AgentAPIView):
    renderer_classes = [PlainEnvelopeRenderer]

    def get(self, request, website_id):
        website = self.website()
        page, next_cursor = chats.list_chats(
            request.user, website,
            q=request.query_params.get("q", ""),
            cursor=request.query_params.get("cursor", ""),
        )
        return Response({
            "results": list(ChatSummarySerializer(page, many=True).data),
            "next_cursor": next_cursor,
        })

    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(ChatCreateSerializer, request.data)
        # A chat exists to start a run; without a key none can start.
        if not api_key_configured():
            raise AgentNotConfigured()
        chat = chats.create_chat(request.user, website, entry_agent=data["entry_agent"])
        return Response(chat_detail(chats.chat_view(chat)), status=201)


class ChatDetailView(AgentAPIView):
    def get(self, request, website_id, chat_id):
        chat = chats.get_chat(request.user, self.website(), chat_id)
        return Response(chat_detail(chats.chat_view(chat)))

    def patch(self, request, website_id, chat_id):
        chat = chats.get_chat(request.user, self.website(write=True), chat_id)
        data = _validated(ChatUpdateSerializer, request.data)
        # The switch can be refused (409), so it goes first.
        if "current_leaf_id" in data:
            chats.switch_branch(chat, data["current_leaf_id"])
        if "title" in data:
            chats.rename_chat(chat, data["title"])
        return Response(chat_detail(chats.chat_view(chat)))

    def delete(self, request, website_id, chat_id):
        chat = chats.get_chat(request.user, self.website(write=True), chat_id)
        chats.delete_chat(chat)
        return Response(status=204)


class MessageFeedbackView(AgentAPIView):
    def post(self, request, website_id, message_id):
        website = self.website(write=True)
        data = _validated(FeedbackSerializer, request.data)
        value = chats.set_feedback(
            request.user, website, message_id,
            value=data["value"], reason=data["reason"], comment=data["comment"],
        )
        return Response({"value": value})


# ── Artifacts and reports ─────────────────────────────────────────────────


class ArtifactListView(AgentAPIView):
    def get(self, request, website_id):
        website = self.website()
        kinds = [k.strip() for k in request.query_params.get("kind", "").split(",") if k.strip()]
        unknown = [k for k in kinds if k not in ArtifactKind.values]
        if unknown:
            raise InvalidRequest({"kind": [f"Unknown kind: {', '.join(unknown)}."]})
        items = artifacts.list_artifacts(website, kinds=kinds, q=request.query_params.get("q", ""))
        # Each summary carries its review block (one query for the list).
        prefetch_related_objects(items, "review__reviewer")
        return Response(list(ArtifactSummarySerializer(items, many=True, context={"user": request.user}).data))


class ArtifactDetailView(AgentAPIView):
    def get(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(), artifact_id)
        return Response(artifact_detail(artifact, artifacts.latest_version(artifact), request.user))

    def patch(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(write=True), artifact_id)
        data = _validated(ArtifactUpdateSerializer, request.data)
        artifacts.rename_artifact(artifact, data["title"])
        return Response(artifact_detail(artifact, artifacts.latest_version(artifact), request.user))

    def delete(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(write=True), artifact_id)
        artifacts.delete_artifact(artifact)
        return Response(status=204)


class ArtifactVersionListView(AgentAPIView):
    def get(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(), artifact_id)
        return Response(list(
            ArtifactVersionSummarySerializer(artifacts.list_versions(artifact), many=True).data
        ))


class ArtifactVersionDetailView(AgentAPIView):
    def get(self, request, website_id, artifact_id, number):
        artifact = artifacts.get_artifact(self.website(), artifact_id)
        return Response(ArtifactVersionSerializer(artifacts.get_version(artifact, number)).data)


class ReportCreateView(AgentAPIView):
    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(ReportCreateSerializer, request.data)
        artifact = reports.save_report(
            request.user, website, message_id=data["message_id"], title=data["title"],
        )
        return Response(
            artifact_detail(artifact, artifacts.latest_version(artifact), request.user),
            status=201,
        )


# ── Research agents and findings ──────────────────────────────────────────


class SwarmDetailView(AgentAPIView):
    """One of the user's research swarms: plan, tasks and versions."""

    def get(self, request, website_id, swarm_id):
        swarm = swarms.get_swarm(request.user, self.website(), swarm_id)
        return Response(swarm_detail(swarm, swarms.list_versions(swarm)))


class SwarmVersionView(AgentAPIView):
    """One version of a swarm, with its results table."""

    def get(self, request, website_id, swarm_id, number):
        swarm = swarms.get_swarm(request.user, self.website(), swarm_id)
        return Response(swarm_version(swarm, swarms.get_version(swarm, number)))


def _findings_data(items, user) -> list[dict]:
    return list(FindingSerializer(items, many=True, context={"user": user}).data)


class FindingListCreateView(AgentAPIView):
    renderer_classes = [PlainEnvelopeRenderer]

    def get(self, request, website_id):
        website = self.website()
        params = _validated(FindingListParamsSerializer, request.query_params)
        rows, next_cursor, counts = findings.list_findings(website, **params)
        return Response({
            "results": _findings_data(rows, request.user),
            "next_cursor": next_cursor,
            "counts": counts,
            "can_edit": can_write(request.user, website),
        })

    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(FindingCreateSerializer, request.data)
        if "from_swarm" in data:
            source = data["from_swarm"]
            created, duplicates = findings.create_from_swarm(
                request.user, website, swarm_id=source["swarm_id"], version=source["version"],
                sheet=source["sheet"], row_keys=source["row_keys"],
            )
        else:
            created = [findings.create_manual(
                request.user, website, title=data["title"], category=data["category"],
                severity=data["severity"], detail=data["detail"],
                prompt_text=data["prompt_text"],
            )]
            duplicates = []
        return Response({
            "created": _findings_data(created, request.user),
            "duplicates": _findings_data(duplicates, request.user),
        }, status=201)


class FindingDetailView(AgentAPIView):
    def patch(self, request, website_id, finding_id):
        finding = findings.get_finding(self.website(write=True), finding_id)
        data = _validated(FindingUpdateSerializer, request.data)
        finding = findings.update_finding(finding, request.user, **data)
        return Response(FindingSerializer(finding, context={"user": request.user}).data)

    def delete(self, request, website_id, finding_id):
        finding = findings.get_finding(self.website(write=True), finding_id)
        findings.delete_finding(finding)
        return Response(status=204)


# ── Project data ──────────────────────────────────────────────────────────


class SuggestionsView(AgentAPIView):
    def get(self, request, website_id):
        return Response(suggestions.suggestions_for(self.website()))


class ActivityView(AgentAPIView):
    def get(self, request, website_id):
        return Response(activity.activity_for(request.user, self.website()))


class BrowseView(AgentAPIView):
    """Fetch a page for the browser pane (agent process only)."""

    live = True

    def post(self, request, website_id):
        self.website()
        data = _validated(BrowseSerializer, request.data)
        user_id = request.user.id
        bucket = TokenBucket(f"agent-browse:{user_id}", capacity=30, refill_per_second=0.5)
        if not bucket.try_acquire():
            raise RateLimited()
        if not browse_quota.consume(user_id):
            raise BrowseLimitReached(browse_quota.limit(user_id))
        return Response(browse.fetch_page(data["url"]))


# ── Helpers ───────────────────────────────────────────────────────────────


def _validated(serializer_class, data) -> dict:
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        raise InvalidRequest(serializer.errors)
    return serializer.validated_data


def _first_error(errors) -> str:
    if isinstance(errors, dict):
        for value in errors.values():
            found = _first_error(value)
            if found:
                return found
    elif isinstance(errors, list):
        for value in errors:
            found = _first_error(value)
            if found:
                return found
    elif errors:
        return str(errors)
    return ""
