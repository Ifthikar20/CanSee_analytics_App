from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OAuthConfig:
    """OAuth credential settings keys for an integration."""
    client_id_setting: str
    client_secret_setting: str
    scopes: list[str] = field(default_factory=list)
    token_url: str = ""
    authorize_url: str = ""
    token_expiry_seconds: int | None = None


@dataclass(frozen=True)
class ApiKeyConfig:
    """API-key credential settings keys for an integration."""
    api_key_setting: str
    api_secret_setting: str = ""


@dataclass(frozen=True)
class IntegrationConfig:
    """Credentials and token refresh for a single integration."""
    name: str               # e.g. "gsc"
    display_name: str        # e.g. "Google Search Console"
    auth_type: str           # "oauth2", "api_key", "api_token"

    # Credentials — exactly one of these should be set based on auth_type
    oauth: OAuthConfig | None = None
    api_key: ApiKeyConfig | None = None

    # Optional callable that refreshes an Integration's OAuth tokens in place.
    # Signature: (integration: Integration) -> None. Called by
    # `apps.websites.tasks.refresh_expiring_tokens` when token_expires_at is
    # within the refresh buffer. Set to None for non-OAuth integrations.
    refresh_token_fn: Callable | None = None

    def get_credentials(self):
        """Load credential values from Django settings.

        Returns a dict with the resolved values (not the setting names).
        """
        from django.conf import settings as django_settings

        if self.auth_type == "oauth2" and self.oauth:
            return {
                "client_id": getattr(django_settings, self.oauth.client_id_setting, ""),
                "client_secret": getattr(django_settings, self.oauth.client_secret_setting, ""),
                "scopes": self.oauth.scopes,
                "token_url": self.oauth.token_url,
                "authorize_url": self.oauth.authorize_url,
            }
        elif self.auth_type == "api_key" and self.api_key:
            result = {
                "api_key": getattr(django_settings, self.api_key.api_key_setting, ""),
            }
            if self.api_key.api_secret_setting:
                result["api_secret"] = getattr(
                    django_settings, self.api_key.api_secret_setting, ""
                )
            return result
        return {}

    def is_configured(self) -> bool:
        """Check if the required credentials are set in Django settings."""
        creds = self.get_credentials()
        if self.auth_type == "oauth2":
            return bool(creds.get("client_id") and creds.get("client_secret"))
        elif self.auth_type == "api_key":
            return bool(creds.get("api_key"))
        return False
