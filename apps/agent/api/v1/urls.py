"""Routes under /api/v1/agent/ (design/agent_contract.md section 1).

Everything under live/ is served by the agent process: nginx sends that
prefix to the agent service, and those views refuse to run elsewhere.
"""
from django.urls import path

from apps.agent.api.v1 import command_views, context_views, review_views, views
from apps.agent.api.v1.live_views import (
    CancelRunView,
    ContinueMessageView,
    EditMessageView,
    RegenerateMessageView,
    RunEventsView,
    SendMessageView,
    SwarmRerunView,
)
from apps.agent.api.v1.stream import run_stream

WEBSITE = "websites/<uuid:website_id>/"
LIVE = "live/"

urlpatterns = [
    path("status/", views.AgentStatusView.as_view(), name="agent-status"),
    path("agents/", views.AgentCatalogView.as_view(), name="agent-catalog"),
    # The usage dashboard (signed in; not gated by AGENT_ENABLED)
    path("command-center/", command_views.CommandCenterView.as_view(),
         name="agent-command-center"),
    # Chats and messages (private to their user)
    path(f"{WEBSITE}chats/", views.ChatListCreateView.as_view(), name="agent-chats"),
    path(
        f"{WEBSITE}chats/<uuid:chat_id>/",
        views.ChatDetailView.as_view(),
        name="agent-chat-detail",
    ),
    path(
        f"{WEBSITE}messages/<uuid:message_id>/feedback/",
        views.MessageFeedbackView.as_view(),
        name="agent-message-feedback",
    ),
    # Artifacts and reports (shared with the website's members)
    path(f"{WEBSITE}artifacts/", views.ArtifactListView.as_view(), name="agent-artifacts"),
    path(
        f"{WEBSITE}artifacts/<uuid:artifact_id>/",
        views.ArtifactDetailView.as_view(),
        name="agent-artifact-detail",
    ),
    path(
        f"{WEBSITE}artifacts/<uuid:artifact_id>/versions/",
        views.ArtifactVersionListView.as_view(),
        name="agent-artifact-versions",
    ),
    # "number", not "version": DRF's URLPathVersioning owns that kwarg.
    path(
        f"{WEBSITE}artifacts/<uuid:artifact_id>/versions/<int:number>/",
        views.ArtifactVersionDetailView.as_view(),
        name="agent-artifact-version",
    ),
    path(f"{WEBSITE}reports/", views.ReportCreateView.as_view(), name="agent-reports"),
    # Reviews (design/p2_contract.md 13c)
    path(
        f"{WEBSITE}artifacts/<uuid:artifact_id>/review/",
        review_views.ArtifactReviewView.as_view(),
        name="agent-artifact-review",
    ),
    path(
        f"{WEBSITE}artifacts/<uuid:artifact_id>/review/comments/",
        review_views.ArtifactReviewCommentsView.as_view(),
        name="agent-artifact-review-comments",
    ),
    path(f"{WEBSITE}reviews/", review_views.ReviewListView.as_view(), name="agent-reviews"),
    path(f"{WEBSITE}team/", review_views.TeamView.as_view(), name="agent-team"),
    # Research agents (private to their chat's user) and findings (shared)
    path(
        f"{WEBSITE}swarms/<uuid:swarm_id>/",
        views.SwarmDetailView.as_view(),
        name="agent-swarm-detail",
    ),
    path(
        f"{WEBSITE}swarms/<uuid:swarm_id>/versions/<int:number>/",
        views.SwarmVersionView.as_view(),
        name="agent-swarm-version",
    ),
    path(f"{WEBSITE}findings/", views.FindingListCreateView.as_view(), name="agent-findings"),
    path(
        f"{WEBSITE}findings/<uuid:finding_id>/",
        views.FindingDetailView.as_view(),
        name="agent-finding-detail",
    ),
    # Project data; the context (design/p2_contract.md 11b)
    path(f"{WEBSITE}context/", context_views.ContextView.as_view(), name="agent-context"),
    path(
        f"{WEBSITE}context/entries/",
        context_views.ContextEntriesView.as_view(),
        name="agent-context-entries",
    ),
    # Before entries/<uuid>/ (the order the contract lists them in).
    path(
        f"{WEBSITE}context/entries/review/",
        context_views.ContextReviewView.as_view(),
        name="agent-context-review",
    ),
    path(
        f"{WEBSITE}context/entries/<uuid:entry_id>/",
        context_views.ContextEntryView.as_view(),
        name="agent-context-entry",
    ),
    path(
        f"{WEBSITE}context/entries/<uuid:entry_id>/references/",
        context_views.ContextReferencesView.as_view(),
        name="agent-context-references",
    ),
    path(
        f"{WEBSITE}context/references/<uuid:reference_id>/",
        context_views.ContextReferenceView.as_view(),
        name="agent-context-reference",
    ),
    path(
        f"{WEBSITE}context/documents/",
        context_views.ContextDocumentsView.as_view(),
        name="agent-context-documents",
    ),
    path(
        f"{WEBSITE}context/documents/<uuid:document_id>/",
        context_views.ContextDocumentView.as_view(),
        name="agent-context-document",
    ),
    path(f"{WEBSITE}suggestions/", views.SuggestionsView.as_view(), name="agent-suggestions"),
    path(f"{WEBSITE}activity/", views.ActivityView.as_view(), name="agent-activity"),
    # Agent process
    path(
        f"{LIVE}{WEBSITE}chats/<uuid:chat_id>/messages/",
        SendMessageView.as_view(),
        name="agent-live-send",
    ),
    path(
        f"{LIVE}{WEBSITE}messages/<uuid:message_id>/regenerate/",
        RegenerateMessageView.as_view(),
        name="agent-live-regenerate",
    ),
    path(
        f"{LIVE}{WEBSITE}messages/<uuid:message_id>/edit/",
        EditMessageView.as_view(),
        name="agent-live-edit",
    ),
    path(
        f"{LIVE}{WEBSITE}messages/<uuid:message_id>/continue/",
        ContinueMessageView.as_view(),
        name="agent-live-continue",
    ),
    path(
        f"{LIVE}{WEBSITE}swarms/<uuid:swarm_id>/rerun/",
        SwarmRerunView.as_view(),
        name="agent-live-swarm-rerun",
    ),
    path(f"{LIVE}runs/<uuid:run_id>/cancel/", CancelRunView.as_view(), name="agent-live-cancel"),
    path(f"{LIVE}runs/<uuid:run_id>/stream/", run_stream, name="agent-live-stream"),
    path(f"{LIVE}runs/<uuid:run_id>/events/", RunEventsView.as_view(), name="agent-live-events"),
    path(f"{LIVE}{WEBSITE}browse/", views.BrowseView.as_view(), name="agent-live-browse"),
]
