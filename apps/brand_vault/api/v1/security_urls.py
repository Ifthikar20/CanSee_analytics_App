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
        "websites/<uuid:website_id>/agents/",
        v.BrandSecurityAgentsView.as_view(),
        name="brand-security-agents",
    ),
    path(
        "websites/<uuid:website_id>/agents/<str:agent_id>/",
        v.BrandSecurityAgentDetailView.as_view(),
        name="brand-security-agent-detail",
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
    path(
        "websites/<uuid:website_id>/prompts/",
        v.BrandSecurityPromptsView.as_view(),
        name="brand-security-prompts",
    ),
    path(
        "websites/<uuid:website_id>/perception/",
        v.BrandSecurityPerceptionView.as_view(),
        name="brand-security-perception",
    ),
    path(
        "websites/<uuid:website_id>/perception/probe/",
        v.BrandSecurityProbeView.as_view(),
        name="brand-security-probe",
    ),
    path(
        "prompts/<uuid:prompt_id>/",
        v.BrandSecurityPromptDetailView.as_view(),
        name="brand-security-prompt-detail",
    ),
]
