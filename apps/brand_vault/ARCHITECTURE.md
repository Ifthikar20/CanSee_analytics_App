# Brand Vault

Per-tenant store of versioned atomic facts about a brand.

## Models
- `BrandFact` — (subject, predicate, object) triple with confidence,
  lifecycle status (pending / approved / rejected / auto), validity
  window (`version_from` / `version_to`), provenance pointer to a
  `rag.KnowledgeChunk`, optional embedding for retrieval, and product /
  topic / audience tags for filtering.
- `FactRevision` — immutable audit log row written on every status
  transition or supersede.

## Services
- `services/fact_extractor.py` — Anthropic-Claude-driven extraction
  from a `KnowledgeChunk`. Confidence below 0.5 is dropped, 0.9+ is
  auto-approved, otherwise it lands in pending. Idempotent on
  (website, subject, predicate, object) for non-superseded facts.
- `services/fact_versioning.py` — `supersede_fact`, `approve_fact`,
  `reject_fact`. Always writes a `FactRevision`.
- `services/embeddings.py` — thin shim around `apps.rag.services.embedder`
  with a 32-dim deterministic hash fallback so retrieval works without
  an OpenAI key.

## Tasks
- `extract_facts_for_website` — iterate chunks for a website, extract.
- `refresh_fact_embeddings` — daily cron, re-embed missing rows.

## Feature flags
- `BRAND_VAULT_EXTRACTION_ENABLED` — gate LLM extraction calls in tests.

## API (`/api/v1/brand-vault/`)
- `GET websites/<id>/facts/?status=&product_line=&topic=&q=`
- `GET websites/<id>/stats/`
- `POST websites/<id>/extract/`
- `GET facts/<id>/`
- `POST facts/<id>/approve|reject|edit/`

## Deferred
- Promoting the embedding column to pgvector once `apps.rag` migrates.
- LLM-assisted dedupe (paraphrase collapse) at write time.

## Security Perception

`services/security/perception.py` turns security-probe answers
(`LLMRankingAudit.probe_kind == "security"`, see
`apps/llm_ranking/ARCHITECTURE.md`) into dashboard numbers:

- `score_claim_support(result)` matches each extracted claim against
  `BrandFact` rows tagged `topic in (security, privacy)` (falling back to all
  approved facts for fact bases that predate topic tags) and knowledge chunks,
  by embedding similarity. Dispatched per result by the audit cell via
  `tasks.score_security_claims_for_result`, gated on `CLAIM_VERIFICATION_ENABLED`.
- `build_security_perception(website, days=90)` aggregates: mention rate
  (Beta-Binomial smoothed, Wilson 95% CI), claim accuracy (supported / scored
  affirmed claims), unsupported compliance claims, incident claims and how many
  are unconfirmed, fear-framing rate, advised-against rate, private-data
  leakage rate (BS-PRIV-001 hits), trust share (Plackett-Luce strengths of the
  latest probe), cited source classes, open security findings, a per-model
  breakdown and a per-probe trend.

Endpoints: `GET /brand-security/websites/<id>/perception/?days=90` and
`POST /brand-security/websites/<id>/perception/probe/`.

Detectors added for this (all in `services/security/detectors.py`, category
`security`): `BS-SEC-001` unsupported compliance claim (judge `require`),
`BS-SEC-002` unconfirmed security incident (judge `require`), `BS-SEC-003`
advised against on security grounds (judge `confirm`). When the security
extraction pass ran for an answer the detectors quote its claims; otherwise
they fall back to the shared lexicon inside the sentence naming the brand.
`Detector.judge_issues` lets a `require` detector declare which issue codes its
judge may return (`hallucinated_compliance`, `false_incident`, `outdated`,
`unverified`).

The fact extractor now labels every fact with a `topic`
(`security`, `privacy`, `pricing`, `product`, `company`, `other`).
