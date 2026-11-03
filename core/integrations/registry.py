from .config import (
    ApiKeyConfig,
    IntegrationConfig,
    OAuthConfig,
)

# ──────────────────────────────────────────────
# Integration Definitions — single source of truth
# ──────────────────────────────────────────────

GOOGLE_ANALYTICS = IntegrationConfig(
    name="ga",
    display_name="Google Analytics",
    auth_type="oauth2",
    oauth=OAuthConfig(
        client_id_setting="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_setting="GOOGLE_OAUTH_CLIENT_SECRET",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        token_url="https://oauth2.googleapis.com/token",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    ),
)

GA_HOSTED = IntegrationConfig(
    name="ga_hosted",
    display_name="Cansee Google tag",
    auth_type="api_key",
    # Credentials are app-level (service account with Editor on the
    # Cansee-owned GA4 pool property), not per-tenant tokens; the
    # tenant only pastes the generated gtag snippet on their site.
    api_key=ApiKeyConfig(api_key_setting="GA4_SA_CREDENTIALS_JSON"),
)

CLOUDFLARE = IntegrationConfig(
    name="cloudflare",
    display_name="Cloudflare",
    # Tenant-supplied zone API token stored on the Integration row; no
    # server-side credentials, no OAuth, and Cloudflare tokens do not
    # expire, so there is no refresh_token_fn.
    auth_type="api_token",
)

GOOGLE_SEARCH_CONSOLE = IntegrationConfig(
    name="gsc",
    display_name="Google Search Console",
    auth_type="oauth2",
    oauth=OAuthConfig(
        client_id_setting="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_setting="GOOGLE_OAUTH_CLIENT_SECRET",
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
        token_url="https://oauth2.googleapis.com/token",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    ),
)


class IntegrationRegistry:
    """Central registry for all integration configurations.

    Usage:
        config = get_registry().get("gsc")
        if config and config.is_configured():
            creds = config.get_credentials()
    """

    def __init__(self):
        self._integrations: dict[str, IntegrationConfig] = {}

    def register(self, config: IntegrationConfig):
        self._integrations[config.name] = config

    def get(self, name: str) -> IntegrationConfig | None:
        return self._integrations.get(name)


# ── Singleton ──

_registry: IntegrationRegistry | None = None


def get_registry() -> IntegrationRegistry:
    """Get the global integration registry (created once)."""
    global _registry
    if _registry is None:
        _registry = IntegrationRegistry()
        for config in [GOOGLE_ANALYTICS, GA_HOSTED, CLOUDFLARE, GOOGLE_SEARCH_CONSOLE]:
            _registry.register(config)
    return _registry
