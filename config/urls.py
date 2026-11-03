from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from core.views.version import VersionView

urlpatterns = [
    # Admin lives at ADMIN_URL, not a hardcoded "admin/". Moving it off the
    # default path is not a security control -- anyone who can read this
    # repo, or watch a login redirect, knows where it went. What it does buy
    # is silence: /admin/ is the single most-scanned path on the internet,
    # and every one of those requests is a django-axes lockout candidate
    # against a real username. Cutting that noise makes the failures that
    # remain worth reading.
    #
    # The real controls are elsewhere and must not be relaxed because of
    # this: axes lockout (5 attempts), a strong superuser password, and
    # ideally an IP allowlist at nginx before this is exposed long-term.
    path(settings.ADMIN_URL, admin.site.urls),

    # Health checks
    path("health/", include("health_check.urls")),

    # Build identity (unauthenticated; shown in the login page footer)
    path("api/v1/version/", VersionView.as_view(), name="api-version"),

    # API v1
    path("api/v1/auth/", include("apps.accounts.api.v1.urls")),
    path("api/v1/orgs/", include("apps.accounts.api.v1.org_urls")),
    # Internal ops surface for the ftb-min admin server ONLY: gated by
    # X-Admin-Key (settings.ADMIN_OPS_KEY; empty = disabled, all 404s).
    # In production also firewall this prefix to the admin host.
    path("api/v1/internal/admin/", include("apps.accounts.api.v1.admin_urls")),
    path("api/v1/websites/", include("apps.websites.api.v1.urls")),
    path("api/v1/analytics/", include("apps.analytics.api.v1.urls")),
    path("api/v1/notifications/", include("apps.notifications.api.v1.urls")),
    path("api/v1/billing/", include("apps.billing.api.v1.urls")),
    path("api/v1/llm-ranking/", include("apps.llm_ranking.api.v1.urls")),
    path("api/v1/rag/", include("apps.rag.api.v1.urls")),
    path("api/v1/onboarding/", include("apps.onboarding.api.v1.urls")),
    path("api/v1/prompt-library/", include("apps.prompt_library.api.v1.urls")),
    path("api/v1/citations/", include("apps.citations.api.v1.urls")),
    path("api/v1/brand-vault/", include("apps.brand_vault.api.v1.urls")),
    path("api/v1/brand-security/", include("apps.brand_vault.api.v1.security_urls")),
    path("api/v1/search-console/", include("apps.search_console.api.v1.urls")),
    path("api/v1/search-gap/", include("apps.search_gap.api.v1.urls")),
    path("api/v1/assistant/", include("apps.assistant.api.v1.urls")),
    # Agent workspace. Its live/ routes are served by the agent process
    # (nginx sends /api/v1/agent/live/ to the agent service).
    path("api/v1/agent/", include("apps.agent.api.v1.urls")),
    # External traffic sources (GA4 / hosted tag / Cloudflare). NOT under
    # /api/v1/analytics/: that prefix's middleware audit-logs every GET,
    # and this surface is polled every 30s.
    path("api/v1/web-analytics/", include("apps.web_analytics.api.v1.urls")),

    # Pixel ingestion (high throughput)
    path("api/v1/track/", include("apps.analytics.api.v1.pixel_urls")),
]

if settings.DEBUG:
    # The OpenAPI schema and its viewers map every endpoint, so they are
    # served in local development only.
    from drf_spectacular.views import (
        SpectacularAPIView,
        SpectacularRedocView,
        SpectacularSwaggerView,
    )

    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/schema/swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]

    try:
        import debug_toolbar  # noqa: F401
        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
