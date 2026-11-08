from django.urls import path

from apps.prompt_box.api.v1 import views

_W = "websites/<uuid:website_id>/"

urlpatterns = [
    path(f"{_W}estimate/", views.EstimateView.as_view(), name="prompt-box-estimate"),
    path(f"{_W}runs/", views.RunsView.as_view(), name="prompt-box-runs"),
    path(f"{_W}runs/<uuid:run_id>/", views.RunView.as_view(), name="prompt-box-run"),
    path(
        f"{_W}runs/<uuid:run_id>/cancel/",
        views.RunCancelView.as_view(),
        name="prompt-box-run-cancel",
    ),
]
