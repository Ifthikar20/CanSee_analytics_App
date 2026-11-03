"""Routes under /api/v1/agent/ (design/agent_contract.md section 1).

Everything under live/ is served by the agent process: nginx sends that
prefix to the agent service, and those views refuse to run elsewhere.
"""
from django.urls import path

from apps.agent.api.v1 import views
from apps.agent.api.v1.live_views import (
    CancelRunView,
    ContinueMessageView,
    EditMessageView,
    RegenerateMessageView,
    RunEventsView,
    SendMessageView,
)
from apps.agent.api.v1.stream import run_stream

WEBSITE = "websites/<uuid:website_id>/"
LIVE = "live/"

urlpatterns = [
    path("status/", views.AgentStatusView.as_view(), name="agent-status"),
    path("agents/", views.AgentCatalogView.as_view(), name="agent-catalog"),
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
    # Project data
    path(f"{WEBSITE}context/", views.BrandContextView.as_view(), name="agent-context"),
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
    path(f"{LIVE}runs/<uuid:run_id>/cancel/", CancelRunView.as_view(), name="agent-live-cancel"),
    path(f"{LIVE}runs/<uuid:run_id>/stream/", run_stream, name="agent-live-stream"),
    path(f"{LIVE}runs/<uuid:run_id>/events/", RunEventsView.as_view(), name="agent-live-events"),
    path(f"{LIVE}{WEBSITE}browse/", views.BrowseView.as_view(), name="agent-live-browse"),
]
