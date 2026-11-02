from django.urls import path

from apps.search_gap.api.v1 import views

urlpatterns = [
    path(
        "websites/<uuid:website_id>/overview/",
        views.SearchGapOverviewView.as_view(),
        name="search-gap-overview",
    ),
    path(
        "websites/<uuid:website_id>/prompts/<uuid:prompt_id>/",
        views.SearchGapPromptView.as_view(),
        name="search-gap-prompt",
    ),
    path(
        "websites/<uuid:website_id>/prompts/<uuid:prompt_id>/check/",
        views.SearchGapCheckView.as_view(),
        name="search-gap-check",
    ),
]
