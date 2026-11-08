# Brand Vault

Per-tenant store of versioned atomic facts about a brand, plus Brand
Security findings raised from the AI answers the audits store.

## Models
- `BrandFact` — (subject, predicate, object) triple with confidence,
  lifecycle status (pending / approved / rejected / auto), validity
  window (`version_from` / `version_to`), provenance pointer to a
  `rag.KnowledgeChunk`, optional embedding for retrieval, and product /
  topic / audience tags for filtering.
- `FactRevision` — immutable audit log row written on every status
  transition or supersede.
- `SafetyAlert` — one Brand Security finding, linked to the stored answer
  (`result`) it was raised from.
- `BrandSecurityConfig` — per-website brand terms and the LLM judge switch.
- `BrandPulse` — per-website digest settings.
- Retired, kept for existing rows only: `ToneSample` (fed Content Studio),
  `SafetyPrompt` and `BrandSecurityAgent` (the removed external-source
  security agents).

## Services
Facts, alignment and the judge's ground truth only ever read Brand Input
(`source_app = brand_input`, excluding audit context). The knowledge base
also mirrors AI answers and findings for the assistant; those are never
treated as the brand's word.

- `services/fact_extractor.py` — Anthropic-Claude-driven extraction
  from a Brand Input `KnowledgeChunk`. Confidence below 0.5 is dropped,
  0.9+ is auto-approved, otherwise it lands in pending. Idempotent on
  (website, subject, predicate, object) for non-superseded facts.
- `services/fact_versioning.py` — `supersede_fact`, `approve_fact`,
  `reject_fact`. Always writes a `FactRevision`.
- `services/embeddings.py` — `embed_text` returns `(vector, model)` from
  `apps.rag.services.embedder`, or `(None, "")` when embeddings are
  unavailable. There is no fallback vector: the fact keeps a NULL
  embedding until `refresh_fact_embeddings` fills it in.
- `services/brand_alignment.py` — scores a stored answer against the
  brand's facts and chunks; NULL score with an explicit status when it
  cannot be measured.
- `services/security/` — `response_auditor` runs the `detectors`
  registry over stored answers and escalates nuanced findings to the
  LLM `judge`. `brand_terms` resolves the brand's names.
  `sources/reddit.py` serves Brand Research's community lane.

## Tasks
- `extract_facts_for_website` — iterate chunks for a website, extract.
- `refresh_fact_embeddings` — daily cron, embeds facts with no vector or a
  legacy hash vector once an embedding key is configured.
- `audit_recent_results` / `scan_unaudited_responses` — run the response
  auditor after prompt runs, plus an hourly catch-up sweep.
- `analyze_alignment_for_result`, `run_brand_pulse`.

## Feature flags
- `BRAND_VAULT_EXTRACTION_ENABLED` — gate LLM extraction calls in tests.

## API
`/api/v1/brand-vault/`:
- `GET websites/<id>/facts/?status=&product_line=&topic=&q=`
- `GET websites/<id>/stats/`
- `POST websites/<id>/extract/`
- `GET facts/<id>/`
- `POST facts/<id>/approve|reject|edit/`

`/api/v1/brand-security/`:
- `GET taxonomy/`
- `GET websites/<id>/overview/` — `health_score` is null with
  `scan_status: "not_scanned"` until stored answers have been checked.
- `GET websites/<id>/alerts/`, `POST alerts/<id>/resolve|dismiss/`
- `GET|PUT websites/<id>/config/`, `GET|PUT websites/<id>/pulse/`

## Deferred
- Promoting the embedding column to pgvector once `apps.rag` migrates.
- LLM-assisted dedupe (paraphrase collapse) at write time.
