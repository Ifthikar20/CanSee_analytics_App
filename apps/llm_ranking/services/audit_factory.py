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


def configured_providers(requested: list[str] | None = None) -> list[str]:
    """Filter provider keys to those implemented AND configured.

    Unknown keys (stub providers advertised in PROVIDER_CHOICES) are
    dropped so a run can never silently produce no results. Falls back to
    ["claude"] so a misconfigured environment still queues a run.
    """
    keys = requested or list(PROVIDERS.keys())
    configured = [
        k for k in keys
        if k in PROVIDERS and getattr(settings, PROVIDERS[k].api_key_setting, "")
    ]
    return configured or ["claude"]


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
    probe_kind: str = LLMRankingAudit.PROBE_KIND_VISIBILITY,
) -> LLMRankingAudit:
    """Create an audit, falling back to Website row fields when a value
    is omitted. ``region=None`` derives the region from ``location``;
    pass an explicit region (e.g. the client-selected one) to skip that.

    Every construction path spends the creator's monthly prompt
    allowance here — an audit that would overdraw it raises
    ``prompt_allowance_exceeded`` (403) before anything is queued.
    """
    from apps.billing.services.org_entitlements import check_prompt_allowance

    check_prompt_allowance(user, len(list(prompts)))

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
        prompts=list(prompts),
        providers_queried=configured_providers(providers),
        context_urls=list(context_urls or []),
        prompt_source=prompt_source,
        probe_kind=(
            probe_kind
            if probe_kind in dict(LLMRankingAudit.PROBE_KIND_CHOICES)
            else LLMRankingAudit.PROBE_KIND_VISIBILITY
        ),
    )
