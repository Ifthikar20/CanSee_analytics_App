"""Ask Cansee assistant API.

Endpoints:
    GET    /api/v1/assistant/status/                              - available?
    POST   /api/v1/assistant/<website_id>/ask/                    - answer a question
    GET    /api/v1/assistant/<website_id>/conversations/          - list threads
    POST   /api/v1/assistant/<website_id>/conversations/          - open a thread
    GET    /api/v1/assistant/<website_id>/conversations/<id>/     - thread + messages
    PATCH  /api/v1/assistant/<website_id>/conversations/<id>/     - rename
    DELETE /api/v1/assistant/<website_id>/conversations/<id>/     - delete

Tenant isolation is enforced by TenantScopedAPIView.get_website (404/403
unless the caller owns the website) plus user = request.user; no
identifier is ever read from the request body.

Availability: settings.ASSISTANT_ENABLED is a deployment-wide
entitlement/maintenance switch. When it is off, /ask/ returns 503 with
the configured maintenance message and the UI degrades to a maintenance
card instead of the composer.

When no answer can be produced, /ask/ returns 503
``assistant_not_configured`` (no AI provider key) or 502
``assistant_failed`` (the provider call failed), as
{"error": {"code", "message"}}, and stores nothing for that turn.
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assistant.api.v1.serializers import (
    AssistantAskSerializer,
    ConversationDetailSerializer,
    ConversationRenameSerializer,
    ConversationSummarySerializer,
)
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.assistant.services.orchestrator import (
    NOT_CONFIGURED_MESSAGE,
    AssistantUnavailable,
    answer,
    get_assistant_provider,
)
from core.resilience import TokenBucket
from core.views.base import TenantScopedAPIView

# Cap history folded into the prompt regardless of what the client sends.
_MAX_HISTORY = 12


def _maintenance_message() -> str:
    return getattr(
        settings, "ASSISTANT_MAINTENANCE_MESSAGE",
        "Ask Cansee is temporarily unavailable.",
    )


def _is_enabled() -> bool:
    return bool(getattr(settings, "ASSISTANT_ENABLED", True))


def _ask_bucket(user_id) -> TokenBucket:
    """Per-user throttle. Each ask is one LLM call + one embedding, so
    keep it modest: burst 15, ~18/min steady state."""
    return TokenBucket(
        name=f"assistant-ask:{user_id}",
        capacity=15,
        refill_per_second=0.3,
    )


class AssistantStatusView(APIView):
    """Feature availability, so the UI can hide the trigger or show a
    maintenance or "not available yet" card without first firing a
    failing question. ``configured`` is False when no AI provider key
    is set."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        enabled = _is_enabled()
        configured = get_assistant_provider() is not None
        if not enabled:
            message = _maintenance_message()
        elif not configured:
            message = NOT_CONFIGURED_MESSAGE
        else:
            message = ""
        return Response({
            "enabled": enabled,
            "configured": configured,
            "message": message,
        })


class AssistantAskView(TenantScopedAPIView):
    def post(self, request, website_id):
        # Entitlement gate first: when the feature is off nobody reaches
        # the model, regardless of tenant or plan.
        if not _is_enabled():
            return Response(
                {"error": {"code": "assistant_disabled",
                           "message": _maintenance_message()}},
                status=503,
            )

        website = self.get_website(website_id)  # tenant gate (404/403)

        if not _ask_bucket(request.user.id).try_acquire():
            return Response(
                {"error": "You're asking a lot very fast. Give it a few "
                          "seconds and try again."},
                status=429,
            )

        serializer = AssistantAskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        question = data["question"]

        # Resolve the thread this turn belongs to. An unknown or foreign id
        # yields None rather than an error, and the turn opens a fresh
        # thread -- losing a thread pointer should not lose the answer.
        conversation = None
        if data.get("conversation_id"):
            conversation = AssistantConversation.objects.filter(
                pk=data["conversation_id"], website=website, user=request.user,
            ).first()

        # History comes from the stored thread when we have one: the client
        # copy is a mirror, and the database is the record of what was
        # actually said.
        history = []
        if conversation is not None:
            history = [
                {"role": m.role, "content": m.content}
                for m in conversation.messages.all()
            ][-_MAX_HISTORY:]
        if not history:
            history = (data.get("history") or [])[-_MAX_HISTORY:]

        try:
            result = answer(
                user=request.user,
                website=website,
                question=question,
                history=history,
            )
        except AssistantUnavailable as exc:
            # A turn without an answer is not part of the conversation:
            # nothing is stored, and no thread is opened for it.
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=503 if exc.code == AssistantUnavailable.NOT_CONFIGURED else 502,
            )

        with transaction.atomic():
            if conversation is None:
                conversation = AssistantConversation.objects.create(
                    website=website, user=request.user,
                    title=AssistantConversation.title_from(question),
                )
            AssistantMessage.objects.create(
                conversation=conversation, role=AssistantMessage.Role.USER,
                content=question,
            )
            AssistantMessage.objects.create(
                conversation=conversation, role=AssistantMessage.Role.ASSISTANT,
                content=result["answer"],
                grounded=bool(result.get("grounded")),
                model=(result.get("model") or "")[:80],
            )
            fields = ["last_message_at", "updated_at"]
            conversation.last_message_at = timezone.now()
            # A thread opened from the sidebar has no title until its first
            # question arrives.
            if not conversation.title:
                conversation.title = AssistantConversation.title_from(question)
                fields.append("title")
            conversation.save(update_fields=fields)

        return Response({**result, "conversation_id": str(conversation.id)})


class AssistantConversationListView(TenantScopedAPIView):
    """Threads for the sidebar list, newest activity first."""

    # Enough to fill the sidebar without turning the first paint into a
    # scroll of history nobody reads.
    MAX_CONVERSATIONS = 60

    def _queryset(self, website, user):
        # Order explicitly rather than leaning on Meta.ordering: annotate()
        # drops it, which silently returned oldest-first.
        #
        # Coalesce matters too. A thread opened but not yet used has a null
        # last_message_at, and NULL ordering under DESC differs between
        # Postgres (first) and SQLite (last) -- so the brand-new chat you
        # just opened would sit on top in production and at the bottom in
        # the test suite. Falling back to created_at makes "newest first"
        # mean the same thing on both.
        return (
            AssistantConversation.objects
            .filter(website=website, user=user)
            .annotate(message_count=Count("messages"))
            .order_by(Coalesce("last_message_at", "created_at").desc())
        )

    def get(self, request, website_id):
        website = self.get_website(website_id)
        qs = self._queryset(website, request.user)[:self.MAX_CONVERSATIONS]
        return Response({
            "conversations": ConversationSummarySerializer(qs, many=True).data,
        })

    def post(self, request, website_id):
        website = self.get_website(website_id)
        conversation = AssistantConversation.objects.create(
            website=website, user=request.user,
        )
        conversation.message_count = 0
        return Response(
            ConversationSummarySerializer(conversation).data, status=201,
        )


class AssistantConversationDetailView(TenantScopedAPIView):
    def _get(self, request, website_id, conversation_id):
        website = self.get_website(website_id)
        return self.get_tenant_object(
            AssistantConversation.objects
            .filter(website=website, user=request.user)
            .annotate(message_count=Count("messages"))
            .prefetch_related("messages"),
            pk=conversation_id,
        )

    def get(self, request, website_id, conversation_id):
        conversation = self._get(request, website_id, conversation_id)
        return Response(ConversationDetailSerializer(conversation).data)

    def patch(self, request, website_id, conversation_id):
        conversation = self._get(request, website_id, conversation_id)
        serializer = ConversationRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # A blank title falls back to the auto-title so a cleared field
        # shows the question again rather than an unlabelled row.
        title = serializer.validated_data["title"].strip()
        if not title:
            first = conversation.messages.filter(
                role=AssistantMessage.Role.USER,
            ).first()
            title = AssistantConversation.title_from(first.content) if first else ""
        conversation.title = title
        conversation.save(update_fields=["title", "updated_at"])
        return Response(ConversationSummarySerializer(conversation).data)

    def delete(self, request, website_id, conversation_id):
        conversation = self._get(request, website_id, conversation_id)
        conversation.delete()
        return Response(status=204)
