# Citations — Technical Reference

## Purpose

Phase 2 of the GEO platform. After every `LLMRankingResult` is saved,
this app extracts every URL the LLM cited, classifies the source domain
(Reddit, news, Wikipedia, docs, social, your own site, a competitor's
site, etc.) and rolls the data up into share-of-citation snapshots that
power the Source Influence dashboard.

## Data Model

| Model | Purpose |
|-------|---------|
| `Citation` | One URL cited by an LLM in a single `LLMRankingResult`. Carries the normalized URL, apex domain, classified `source_class`, extraction method, and confidence. |
| `DomainClassification` | Cache of `apex_domain -> source_class`. Populated lazily by the rule-based classifier and re-visited by the daily LLM-classification task. |
| `SourceInfluenceSnapshot` | Per-`(provider x industry x website)` rollup over a time window. Built daily by Celery beat. `website` is `NULL` for global benchmark rollups. |

`Citation.normalized_url` is the dedup key. The `(result, normalized_url)`
unique constraint means that re-running the extractor against the same
result is a safe no-op; this is what lets the Celery hook tolerate
retries.

## Services

* `url_normalizer` — canonicalises URLs (lowercase host, strip default
  ports, strip tracking params such as `utm_*`, `fbclid`, `gclid`),
  derives the apex domain via `tldextract` when installed and falls
  back to a curated two-label TLD list otherwise. Idempotent.
* `domain_classifier` — rule-first classifier with a persisted cache.
  Per-tenant overrides for `your_site` / `competitor_site` short-circuit
  the cache so tenants do not see each other's labels. Unknown domains
  fall through to `OTHER` and are revisited by the daily upgrade task.
* `extractors/` — pluggable per-provider extractors:
  * `PerplexityNativeExtractor` reads the structured `citations` field.
  * `GeminiGroundingExtractor` reads `groundingChunks` (or a flattened
    citations list) emitted by Gemini's tool-grounded responses.
  * `RegexExtractor` is the universal fallback — runs a conservative
    URL regex over the response body and assigns lower confidence (0.6).
* `extraction_service` — orchestrates the strategy for a result, dedupes
  by normalized URL keeping the highest-confidence candidate, classifies
  each URL, and persists `Citation` rows via `update_or_create`.
* `snapshot_service` — builds the per-provider breakdown and top-domain
  list either live (one audit, no DB writes) or rolled up into
  `SourceInfluenceSnapshot` rows.

## Pipeline Hook

The audit runner dispatches `apps.citations.tasks.extract_citations_for_result`
immediately after each `LLMRankingResult` is saved. The hook is gated on
`settings.CITATION_EXTRACTION_ENABLED` so the existing ranking-service
test suite can opt out without touching the citations app. Extraction
failures are swallowed and logged: citations are a downstream analytic,
never on the critical path of the audit.

## Celery Beat

* `compute-source-influence` — daily 05:30. Builds global per-provider
  snapshots, then per-website snapshots for every website with citations
  in the trailing window.
* `classify-unknown-domains` — daily 06:00. Re-runs the rule classifier
  against `OTHER`-classified domains from the past 24 hours and upgrades
  any that are now matched.

## API Surface

All endpoints are under `/api/v1/citations/` and require authentication.
Website-scoped endpoints inherit the project's tenant-scoped contract;
audit-scoped endpoints resolve the audit's website first so the same
guard applies.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `audits/<audit_id>/citations/` | Paginated citations for one audit. |
| GET | `audits/<audit_id>/source-influence/` | Live breakdown for one audit. |
| GET | `websites/<website_id>/source-influence/` | Per-website rollup over the trailing window. |
| GET | `websites/<website_id>/citations/` | Cross-audit citation feed for one website. |
| GET | `source-influence/global/` | Global benchmark rollup. |

## Extension Points

* Replace the curated news-domain list with a SerpAPI-driven
  classifier. The cache table accepts `classified_by="llm"` /
  `"manual"` so the upgrade is additive.
* Per-tenant competitor lists currently aggregate from
  `LLMRankingResult.competitors_mentioned`. A dedicated competitor
  registry (with verified URLs) would let the classifier mark
  competitor citations on first seeing the URL rather than after the
  first mention has been recorded.
