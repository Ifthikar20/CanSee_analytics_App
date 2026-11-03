"""Public service API for the llm_ranking app.

Note: modules with colliding names (build_for_user, quota_for_user,
quota_remaining, is_configured, search) are not re-exported here — import
them from their fully-qualified module path to avoid ambiguity.
"""
from apps.llm_ranking.services.business_story import (
    gather_and_ingest,
    gather_story,
    render_story,
)
from apps.llm_ranking.services.citation_geo import (
    aggregate_countries,
    attribute_country,
    merge_country_counts,
)
from apps.llm_ranking.services.competitor_discovery import discover_competitors
from apps.llm_ranking.services.competitor_normalize import canonical_name, display_name
from apps.llm_ranking.services.content_enricher import ContentEnricher
from apps.llm_ranking.services.domain_scanner import scan_domain
from apps.llm_ranking.services.eta_service import (
    estimate_audit_duration,
    schedule_eta,
)
from apps.llm_ranking.services.extraction_service import HaikuExtractionService
from apps.llm_ranking.services.geo_stats import (
    build_breakdowns_for_user,
    build_kpis_for_user,
)
from apps.llm_ranking.services.geo_tagger import recommendations_for, tag_prompts
from apps.llm_ranking.services.plackett_luce import (
    fit_plackett_luce,
    rankings_from_results,
)
from apps.llm_ranking.services.prompt_library import (
    funnel_stage_for,
    rationale_for,
)
from apps.llm_ranking.services.ranking_service import (
    LLMRankingService,
    beta_binomial_mean,
    build_enriched_system_prompt,
    wilson_ci,
)
from apps.llm_ranking.services.regions import (
    Region,
    get_region,
    region_for_country,
    region_system_instruction,
    supported_countries,
)
from apps.llm_ranking.services.scan_dispatch import dispatch_scan
from apps.llm_ranking.services.source_prompt_resolver import resolve_source_prompt_id
from apps.llm_ranking.services.visibility_series import build_overview_for_website

__all__ = [
    "gather_and_ingest",
    "gather_story",
    "render_story",
    "aggregate_countries",
    "attribute_country",
    "merge_country_counts",
    "discover_competitors",
    "canonical_name",
    "display_name",
    "ContentEnricher",
    "scan_domain",
    "estimate_audit_duration",
    "schedule_eta",
    "HaikuExtractionService",
    "build_breakdowns_for_user",
    "build_kpis_for_user",
    "recommendations_for",
    "tag_prompts",
    "fit_plackett_luce",
    "rankings_from_results",
    "funnel_stage_for",
    "rationale_for",
    "LLMRankingService",
    "beta_binomial_mean",
    "build_enriched_system_prompt",
    "wilson_ci",
    "Region",
    "get_region",
    "region_for_country",
    "region_system_instruction",
    "supported_countries",
    "dispatch_scan",
    "resolve_source_prompt_id",
    "build_overview_for_website",
]
