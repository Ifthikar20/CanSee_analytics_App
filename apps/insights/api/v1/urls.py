from django.urls import path

from apps.insights.api.v1 import views

_W = "websites/<uuid:website_id>/"

urlpatterns = [
    # Runs (contract 1e).
    path(f"{_W}runs/options/", views.RunOptionsView.as_view(), name="insights-run-options"),
    path(f"{_W}runs/estimate/", views.RunEstimateView.as_view(), name="insights-run-estimate"),
    path(f"{_W}runs/", views.RunsView.as_view(), name="insights-runs"),
    path(f"{_W}runs/<uuid:run_id>/", views.RunView.as_view(), name="insights-run"),
    path(
        f"{_W}runs/<uuid:run_id>/cancel/",
        views.RunCancelView.as_view(),
        name="insights-run-cancel",
    ),
    # Answer analysis, competitors, citations (sections 3 and 4).
    path(f"{_W}sentiment/", views.SentimentView.as_view(), name="insights-sentiment"),
    path(f"{_W}fact-check/", views.FactCheckView.as_view(), name="insights-fact-check"),
    path(f"{_W}competitors/", views.CompetitorsView.as_view(), name="insights-competitors"),
    path(f"{_W}citations/", views.CitationsView.as_view(), name="insights-citations"),
    # Crawler access and pages (sections 5 and 6).
    path(f"{_W}crawler-access/", views.CrawlerAccessView.as_view(), name="insights-crawler-access"),
    path(f"{_W}pages/", views.PagesView.as_view(), name="insights-pages"),
    # Prompt research (section 7).
    path(f"{_W}prompt-ideas/", views.PromptIdeasView.as_view(), name="insights-prompt-ideas"),
    path(
        f"{_W}prompt-ideas/<uuid:idea_id>/",
        views.PromptIdeaView.as_view(),
        name="insights-prompt-idea",
    ),
    path(
        f"{_W}prompt-ideas/<uuid:idea_id>/track/",
        views.PromptIdeaTrackView.as_view(),
        name="insights-prompt-idea-track",
    ),
    # Query fan-outs (section 8).
    path(f"{_W}fan-outs/", views.FanoutsView.as_view(), name="insights-fan-outs"),
    # Products (section 9).
    path(f"{_W}products/", views.ProductsView.as_view(), name="insights-products"),
    path(
        f"{_W}products/<uuid:product_id>/",
        views.ProductView.as_view(),
        name="insights-product",
    ),
]
