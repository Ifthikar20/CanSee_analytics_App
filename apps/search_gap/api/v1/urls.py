from django.urls import path

from apps.search_gap.api.v1 import country_views, views

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
    # Rankings by country.
    path(
        "websites/<uuid:website_id>/countries/",
        country_views.CountryMatrixView.as_view(),
        name="search-gap-countries",
    ),
    path(
        "websites/<uuid:website_id>/countries/options/",
        country_views.CountryOptionsView.as_view(),
        name="search-gap-country-options",
    ),
    path(
        "websites/<uuid:website_id>/countries/estimate/",
        country_views.CountryEstimateView.as_view(),
        name="search-gap-country-estimate",
    ),
    path(
        "websites/<uuid:website_id>/countries/runs/",
        country_views.CountryRunsView.as_view(),
        name="search-gap-country-runs",
    ),
    path(
        "websites/<uuid:website_id>/countries/runs/<uuid:run_id>/",
        country_views.CountryRunView.as_view(),
        name="search-gap-country-run",
    ),
    path(
        "websites/<uuid:website_id>/countries/runs/<uuid:run_id>/cancel/",
        country_views.CountryRunCancelView.as_view(),
        name="search-gap-country-run-cancel",
    ),
    path(
        "websites/<uuid:website_id>/countries/results/<uuid:result_id>/",
        country_views.CountryResultView.as_view(),
        name="search-gap-country-result",
    ),
]
