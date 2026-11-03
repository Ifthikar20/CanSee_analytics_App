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
      Artifacts, reports and brand context are shared by the website's
      members.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent.api.v1.serializers import (
    ArtifactSummarySerializer,
    ArtifactUpdateSerializer,
    ArtifactVersionSerializer,
    ArtifactVersionSummarySerializer,
    BrandContextSerializer,
    BrandContextWriteSerializer,
    BrowseSerializer,
    ChatCreateSerializer,
    ChatSummarySerializer,
    ChatUpdateSerializer,
    FeedbackSerializer,
    ReportCreateSerializer,
    artifact_detail,
    chat_detail,
)
from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.constants import ArtifactKind
from apps.agent.exceptions import AgentDisabled, AgentNotConfigured, AgentUnavailable
from apps.agent.services import (
    activity,
    artifacts,
    brand_context,
    browse,
    catalog,
    chats,
    reports,
    suggestions,
)
from core.exceptions import CanseeException, PermissionDenied, RateLimited
from core.interceptors.response_envelope import EnvelopeRenderer
from core.permissions.rbac import role_at_least
from core.quota import DailyQuota
from core.resilience import TokenBucket
from core.views.base import TenantScopedAPIView

browse_quota = DailyQuota(
    namespace="agent_browse", setting_name="AGENT_BROWSE_DAILY_LIMIT", default_limit=300,
)


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
        if write and not _can_write(user, website):
            raise PermissionDenied("Viewers can see this project's agent work but not change it.")
        return website


# ── Status and catalog ────────────────────────────────────────────────────


class AgentStatusView(APIView):
    """Whether the agent can run, and what is configured. Never gated."""

    permission_classes = [IsAuthenticated]
    schema = None

    def get(self, request):
        return Response(catalog.status_for(request.user))


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


# ── Project data ──────────────────────────────────────────────────────────


class BrandContextView(AgentAPIView):
    def get(self, request, website_id):
        return Response(BrandContextSerializer(brand_context.context_for(self.website())).data)

    def put(self, request, website_id):
        website = self.website(write=True)
        data = _validated(BrandContextWriteSerializer, request.data)
        context = brand_context.save_context(website, request.user, **data)
        return Response(BrandContextSerializer(context).data)


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


def _can_write(user, website) -> bool:
    if website.user_id == user.id:
        return True
    if not website.organization_id:
        return False
    role = (
        user.org_memberships.filter(organization_id=website.organization_id)
        .values_list("role", flat=True)
        .first()
    )
    return bool(role) and role_at_least(role, "member")
