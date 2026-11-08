# RAG — Retrieval-Augmented Knowledge Base

## Purpose

Maintain a per-website knowledge base of scraped content
(websites, blogs, product pages, audit context) with embeddings, so
prompts and provider system messages can be **grounded in real
business facts retrieved by semantic similarity**.

This replaces the previous one-shot enrichment model (scrape one URL,
inject into one audit) with an incremental store that **learns over
time**: every audit feeds its enrichment context back into the
knowledge base, so subsequent audits draw on a richer corpus.

## Core flow

```
User adds URL  ──┐
                 │
Audit enrichment ┴──> ingest_url / ingest_site
                          │
                          ├── domain_scanner (HTML -> text)
                          ├── chunker (split + overlap)
                          ├── embedder (OpenAI; pending when unavailable)
                          └── KnowledgeSource + KnowledgeChunk rows
                                            │
                                            ▼
Prompt creation  ─────► retrieve(query=...) ──► top-k chunks
Audit per-cell   ─────► retrieve(query=prompt) ──► sys-prompt context
```

## Models

| Model | Purpose |
|---|---|
| `KnowledgeSource` | One ingested URL belonging to a website (`user` records who added it). Tracks status, kind, content_hash for change detection, chunk_count. Unique on (website, url). |
| `KnowledgeChunk` | Text segment from a KnowledgeSource. Stores the embedding as a JSON list of floats plus model name + dimension. `embedding_model` is `pending` (empty vector) when embeddings were unavailable at ingest, and `hash-256` on legacy word-hash rows; neither is ever scored. |

## Services

### `chunker.py`
- Splits text on explicit section markers (`=== HEADING ===`, markdown headings, `---`)
- Inside each section, slides a 300-word window with 40-word overlap
- Token counts approximated as `words / 0.75` — no tokenizer dependency

### `embedder.py`
- OpenAI `text-embedding-3-small` (1536 dim) via the existing OpenAI SDK, which retries transient errors
- No fallback: without a key, with a spent AI allowance, or when the call fails, it raises `EmbeddingUnavailable` with a reason code (`embeddings_not_configured`, `ai_allowance_exhausted`, `embeddings_failed`)
- Token usage logged via `core.ai_tracking.record_usage` so embedding spend appears alongside provider + extraction spend

### `crawler.py`
- Builds on `apps.llm_ranking.services.domain_scanner.scan_domain`
- Adds `sitemap.xml` / `sitemap_index.xml` discovery
- Same-domain link extraction from a seed page (depth 1 by default)
- Configurable `page_cap` so a crawl of a large site doesn't run away
- Returns one scan dict per page in the same shape the rest of the code already understands

### `ingest_service.py`
- `ingest_url(user, website, url, kind, title, text=None)` — single page
- `ingest_site(user, website, seed_url, page_cap, depth)` — crawl + bulk ingest
- `ingest_audit_context(user, website, audit_id, llm_context)` — feeds an audit's enrichment back into the KB so the next audit draws on it (the "learning" loop)
- Content-hash short-circuit: an unchanged page is not re-chunked; only its missing vectors are filled in
- When embeddings are unavailable the chunks are still stored, without a vector and marked `pending`
- `embed_pending_chunks(...)` embeds pending and legacy hash chunks in place (chunk ids, and the facts linked to them, are kept)
- All chunk writes are transactional: old chunks for a source are deleted and new ones bulk-inserted

### `retriever.py`
- `retrieve(user, website, query, top_k, kinds)` — cosine similarity over the website's model-embedded chunks; returns `RetrievalHits` (a list) with a `reason`
- `reason` is `None` after a normal search; when the search cannot run the hits are empty and `reason` is an embedder code or `embeddings_pending` (chunks exist, none has a vector yet)
- `retrieve_context_block(...)` / `format_context_block(hits)` — retrieved chunks as a system-prompt-ready block under a `=== KNOWLEDGE BASE ===` header, or `""`

## API

All endpoints are `TenantScopedAPIView` — authenticated + website-scoped.

| Method | Path | View | Purpose |
|---|---|---|---|
| GET  | `/api/v1/rag/<wid>/sources/` | `KnowledgeSourceListView` | List sources for current user + website |
| POST | `/api/v1/rag/<wid>/sources/` | `KnowledgeSourceListView` | Enqueue ingest of a single URL or a full crawl |
| GET  | `/api/v1/rag/<wid>/sources/<sid>/` | `KnowledgeSourceDetailView` | Source + its chunks |
| DELETE | `/api/v1/rag/<wid>/sources/<sid>/` | `KnowledgeSourceDetailView` | Remove source + chunks |
| POST | `/api/v1/rag/<wid>/retrieve/` | `RetrieveView` | Top-k retrieval against the KB; `reason` explains an empty result that is not a plain no-match |

## Celery tasks

Both run on the default queue (light HTTP + DB; no need for the `ai`
queue's isolation).

| Task | Purpose |
|---|---|
| `apps.rag.tasks.ingest_url` | Background ingest of a single URL |
| `apps.rag.tasks.ingest_site` | Crawl + ingest |

## Integration with LLM Ranking

Two integration points in `apps.llm_ranking.services.ranking_service`:

1. **`generate_prompts`** — calls `retrieve_context_block` with a query
   like *"buyer questions and use cases for {business} in {industry}"*
   before asking Claude for variant prompts. Variants are now grounded
   in the actual KB content, not just the description string.

2. **`run_audit_cell` and legacy `run_audit`** — for each (prompt,
   provider) cell, retrieves the top-4 chunks most relevant to the
   *prompt itself* and appends them under the static enrichment block
   in the system prompt.

3. **`prepare_audit` / legacy `run_audit`** — after enrichment, the
   combined `llm_context` is ingested back into the KB as a
   `KIND_AUDIT_CONTEXT` source. The next audit's retriever sees it.

All three integrations are in `try / except` blocks so an empty KB or
a transient retrieval failure cannot break audit execution.

## Why not pgvector / Pinecone

The per-user corpus is small (typically < 1000 chunks even after
months of audits). Brute-force cosine over a JSONField is < 100ms at
that scale and avoids:

- A new infrastructure dependency
- Schema lock-in on a particular vector index type
- Cross-tenant index contention

If a single user's corpus exceeds ~10K chunks, swap candidate
selection for pgvector + an HNSW index without changing call sites —
`retrieve()` is the only function that reads embeddings.

## Management commands

- `embed_pending_chunks [--website <id>] [--dry-run]` — embeds chunks stored
  without a vector (and legacy hash vectors) once embeddings work.
- `rag_reindex` — mirrors model vectors into the optional vector index.

## Embedding model upgrade path

`KnowledgeChunk.embedding_model` and `embedding_dim` are stored per
chunk. Until a re-embed completes, `retrieve()` skips chunks whose
embedding dimension doesn't match the current query embedding.

## Tests

| File | Coverage |
|---|---|
| `tests/test_chunker.py` | Section detection, overlap, default labels |
| `tests/test_embedder.py` | Similarity math |
| `tests/test_retriever.py` | Relevance ranking, user/website scoping, kind filter, context block formatting |
