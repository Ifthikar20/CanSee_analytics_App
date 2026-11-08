"""Single construction path for LLMRankingAudit rows.

Both audit entry points — the llm_ranking create endpoint and
prompt_library's auto-scan after saving prompts — build audits here, so
every audit carries the same fields (keywords, context_urls,
prompt_source, provider filter) regardless of which surface queued it.
"""
from __future__ import annotations

from django.conf import settings

from apps.llm_ranking.models import LLMRankingAudit
from apps.llm_ranking.providers import PROVIDERS
from apps.llm_ranking.services.regions import region_for_country


class NoLLMProviderConfigured(Exception):
    """No requested AI engine has an API key, so nothing can be asked.

    ``missing`` names the settings that would enable the engines. Views
    answer with ``response_body()`` and HTTP 409.
    """

    code = "no_llm_provider_configured"

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__("No AI engine is configured.")

    def response_body(self) -> dict:
        return {"error": self.code, "missing": self.missing}


def _implemented(requested: list[str] | None) -> list[str]:
    keys = [k for k in (requested or []) if k in PROVIDERS]
    return keys or list(PROVIDERS.keys())


def configured_providers(requested: list[str] | None = None) -> list[str]:
    """Filter provider keys to those implemented AND configured.

    Unknown keys (stub providers advertised in PROVIDER_CHOICES) are
    dropped. Returns [] when none of the requested engines has a key.
    """
    keys = requested or list(PROVIDERS.keys())
    return [
        k for k in keys
        if k in PROVIDERS and getattr(settings, PROVIDERS[k].api_key_setting, "")
    ]


def missing_provider_keys(requested: list[str] | None = None) -> list[str]:
    """API key settings that are unset for the requested engines."""
    return [
        PROVIDERS[k].api_key_setting
        for k in _implemented(requested)
        if not getattr(settings, PROVIDERS[k].api_key_setting, "")
    ]


def require_configured_providers(requested: list[str] | None = None) -> list[str]:
    """``configured_providers`` that raises when the result is empty."""
    providers = configured_providers(requested)
    if not providers:
        raise NoLLMProviderConfigured(missing_provider_keys(requested))
    return providers


def create_audit(
    *,
    website,
    user,
    prompts,
    providers: list[str] | None = None,
    business_name: str = "",
    business_description: str = "",
    industry: str = "",
    location: str = "",
    region: str | None = None,
    keywords: list | None = None,
    context_urls: list | None = None,
    prompt_source: str = "library",
) -> LLMRankingAudit:
    """Create an audit, falling back to Website row fields when a value
    is omitted. ``region=None`` derives the region from ``location``;
    pass an explicit region (e.g. the client-selected one) to skip that.

    Raises ``NoLLMProviderConfigured`` when no requested engine has a key.
    Every construction path spends the creator's monthly prompt
    allowance here — an audit that would overdraw it raises
    ``prompt_allowance_exceeded`` (403) before anything is queued. The
    audit asks each prompt of each engine: that many prompt runs must be
    left on the project owner's account, or it raises
    ``insufficient_prompt_runs`` / ``prompt_runs_exhausted`` (403).
    """
    from apps.billing.services.org_entitlements import check_prompt_allowance
    from apps.metering.services import prompt_runs

    prompts = list(prompts)
    providers_queried = require_configured_providers(providers)
    check_prompt_allowance(user, len(prompts))
    prompt_runs.require(user, website, len(prompts) * len(providers_queried))

    return LLMRankingAudit.objects.create(
        website=website,
        created_by=user,
        business_name=(
            business_name
            or getattr(website, "business_name", None)
            or website.name
            or ""
        ),
        business_description=(
            business_description or getattr(website, "description", "") or ""
        ),
        industry=industry or getattr(website, "industry", "") or "",
        location=location or "",
        region=region if region is not None else region_for_country(location),
        keywords=list(keywords if keywords is not None else (website.topics or [])),
        prompts=prompts,
        providers_queried=providers_queried,
        context_urls=list(context_urls or []),
        prompt_source=prompt_source,
    )
