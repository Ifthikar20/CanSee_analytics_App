from django.urls import path

from apps.autopilot.api.v1 import views

_W = "websites/<uuid:website_id>/"

urlpatterns = [
    path(_W, views.SettingsView.as_view(), name="autopilot-settings"),
    path(f"{_W}run/estimate/", views.RunEstimateView.as_view(), name="autopilot-run-estimate"),
    path(f"{_W}run/", views.RunCreateView.as_view(), name="autopilot-run-create"),
    path(f"{_W}runs/", views.RunsView.as_view(), name="autopilot-runs"),
    path(f"{_W}runs/<uuid:run_id>/", views.RunView.as_view(), name="autopilot-run"),
    path(
        f"{_W}runs/<uuid:run_id>/cancel/",
        views.RunCancelView.as_view(),
        name="autopilot-run-cancel",
    ),
    path(f"{_W}text/test/", views.TextTestView.as_view(), name="autopilot-text-test"),
    path(f"{_W}ranking/", views.RankingView.as_view(), name="autopilot-ranking"),
    path("links/<str:code>/", views.LinkView.as_view(), name="autopilot-link"),
]
