"""URL routes for the /api/v1/brand-security/ surface."""
from django.urls import path

from . import security_views as v

urlpatterns = [
    path(
        "taxonomy/",
        v.BrandSecurityTaxonomyView.as_view(),
        name="brand-security-taxonomy",
    ),
    path(
        "websites/<uuid:website_id>/overview/",
        v.BrandSecurityOverviewView.as_view(),
        name="brand-security-overview",
    ),
    path(
        "websites/<uuid:website_id>/alerts/",
        v.BrandSecurityAlertsView.as_view(),
        name="brand-security-alerts",
    ),
    path(
        "alerts/<uuid:alert_id>/<str:action>/",
        v.BrandSecurityAlertActionView.as_view(),
        name="brand-security-alert-action",
    ),
    path(
        "websites/<uuid:website_id>/config/",
        v.BrandSecurityConfigView.as_view(),
        name="brand-security-config",
    ),
    path(
        "websites/<uuid:website_id>/pulse/",
        v.BrandPulseView.as_view(),
        name="brand-pulse-config",
    ),
]
