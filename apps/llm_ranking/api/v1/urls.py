from django.urls import path

from . import dashboard_views, views

urlpatterns = [
    path("<uuid:website_id>/audits/", views.LLMRankingAuditListView.as_view(), name="llm-ranking-list"),
    path("<uuid:website_id>/audits/preflight/", views.LLMRankingPreflightView.as_view(), name="llm-ranking-preflight"),
    path("<uuid:website_id>/preview-prompts/", views.LLMRankingPreviewPromptsView.as_view(), name="llm-ranking-preview-prompts"),
    path("<uuid:website_id>/scan-url/", views.ScanURLView.as_view(), name="llm-ranking-scan-url"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/", views.LLMRankingAuditDetailView.as_view(), name="llm-ranking-detail"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/run/", views.LLMRankingAuditRunView.as_view(), name="llm-ranking-run"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/logs/", views.LLMRankingAuditLogsView.as_view(), name="llm-ranking-logs"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/breakdown/", views.LLMRankingProviderBreakdownView.as_view(), name="llm-ranking-breakdown"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/recommendations/", views.LLMRankingRecommendationsView.as_view(), name="llm-ranking-recommendations"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/prompts/", views.LLMRankingPromptResultsView.as_view(), name="llm-ranking-prompt-results"),
    path("<uuid:website_id>/audits/<uuid:audit_id>/providers/<str:provider>/", views.LLMRankingProviderDetailView.as_view(), name="llm-ranking-provider-detail"),
    path("<uuid:website_id>/usage/", views.LLMRankingUsageView.as_view(), name="llm-ranking-usage"),
    path("<uuid:website_id>/provider-health/", views.LLMRankingProviderHealthView.as_view(), name="llm-ranking-provider-health"),
    path("<uuid:website_id>/model-variants/", views.ModelVariantsView.as_view(), name="llm-ranking-model-variants"),
    path("<uuid:website_id>/history/", views.LLMRankingHistoryView.as_view(), name="llm-ranking-history"),
    path("<uuid:website_id>/schedule/", views.LLMRankingScheduleView.as_view(), name="llm-ranking-schedule"),
    path("<uuid:website_id>/schedule/eta/", views.LLMRankingScheduleETAView.as_view(), name="llm-ranking-schedule-eta"),
    path("<uuid:website_id>/schedule/run-now/", views.LLMRankingScheduleRunNowView.as_view(), name="llm-ranking-schedule-run-now"),
    path("<uuid:website_id>/geo-tags/", views.LLMRankingGEOTagsView.as_view(), name="llm-ranking-geo-tags"),
    # Dashboard Visibility Overview card
    path(
        "<uuid:website_id>/visibility-overview/",
        views.VisibilityOverviewView.as_view(),
        name="llm-ranking-visibility-overview",
    ),
    path(
        "<uuid:website_id>/dashboard-summary/",
        views.DashboardSummaryView.as_view(),
        name="llm-ranking-dashboard-summary",
    ),
    path(
        "<uuid:website_id>/visibility-dashboard/",
        dashboard_views.VisibilityDashboardView.as_view(),
        name="llm-ranking-visibility-dashboard",
    ),
]
