# Prompt Library — Technical Reference

## Purpose

Captures real-world demand-side prompts (questions buyers actually ask AI
assistants) and feeds them into the LLM Ranking audit pipeline. Replaces
the supply-side "guess what your customers will type" approach with a
mined corpus organised by a flat 20-row industry taxonomy.

## Data Model

| Model | Purpose |
|-------|---------|
| `Industry` | One of 20 broad industry buckets. Flat — no hierarchy. |
| `Prompt` | A single demand-side prompt, tied to an industry and an intent bucket (`category`, `comparison`, `problem`, `local`). |
| `PromptVariation` | LLM-generated paraphrase of a parent prompt, optionally embedded for near-duplicate detection. |
| `PromptSampleRun` | Per-audit snapshot — which prompts were sampled, with the seed used so the sample is reproducible. |
| `PromptSampleEntry` | Through table preserving sample order. |

`Prompt.text_hash` is a sha256 of the normalised text. Combined with the
`(industry, text_hash)` unique constraint it gives cheap exact-dedup at
the DB layer. There is no near-duplicate detection.

## Services

* `miner_service` — pluggable miner with `RedditMiner`, `SerpApiMiner`,
  `LLMSynthMiner`. Each miner gracefully no-ops when its credentials
  are missing; the daily Celery task tolerates a half-configured env.
* `sampler_service` — stratified / recent / top-demand sampling. The
  stratified strategy assigns equal slots per intent bucket and
  back-fills under-quota buckets from leftovers. Always seeded for
  reproducibility.
* `scoring_service` — combines source weight, recency decay, and
  variation count into a single `demand_score` in `[0, 100]`.

## Tasks

* `mine_daily_prompts` — runs daily at 04:00; iterates active
  industries and invokes every miner.
* `compute_demand_scores` — runs daily at 05:00; refreshes the
  `demand_score` field across the active prompt set.

## REST API

Mounted at `/api/v1/prompt-library/`:

* `GET industries/` — active industries (no pagination).
* `GET prompts/?industry=<slug>&intent_bucket=&search=` — paginated.
* `POST prompts/preview-sample/` — non-persisted preview for the Run
  Audit modal.
* `POST audits/<audit_id>/use-library-sample/` — persists a
  `PromptSampleRun` on a pending audit and flips its `prompt_source`.
* `GET audits/<audit_id>/sample/` — read the persisted sample for the
  audit detail page.

All endpoints require authentication; audit-scoped endpoints use the
website-ownership check from `WebsiteService.get_for_user`.

## Audit Pipeline Hook

`apps.llm_ranking.services.audit_runner.gather_prompts(audit, prompt_source)`
returns the prompt list for a run based on the audit's `prompt_source`
field (`vault`, `library`, `hybrid`). The Run Audit endpoint calls
`apply_to_audit` immediately before enqueuing the Celery task so the
existing `audit.prompts` JSON field is populated as the source of truth.

`LLMRankingResult.prompt_source_label` carries provenance per-row so
the dashboard can render badges like `Library / Reddit` next to each
result. Legacy rows have an empty label.
