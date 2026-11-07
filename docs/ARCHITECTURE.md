# Cansee Architecture & Deployment Status

Date of report: 2026-04-25
Working copy: `/Users/ifthikaraliseyed/Desktop/FTB_APP/ftb-api-26`
Local HEAD: `11d0456` ("some")
Remote: `git@github.com:Ifthikar20/ftb-api-26.git`

---

## 1. Deployment Status — short answer

**The frontend you see in production is almost certainly stale, and you also have unpushed/uncommitted work locally.** Three independent signals confirm this:

| Signal | Evidence | Implication |
|---|---|---|
| Built bundle older than source | `frontend/dist/index.html` modified `Apr 24 10:30`. `frontend/src/pages/LLMRankingPage.vue` modified `Apr 25 20:14`. | The dist/ artifact was built before the latest UI work. If the prod container is built from the current commit, it would still rebuild — but if the host hasn't pulled, prod is serving the old bundle. |
| Working tree is dirty | `git status` shows modified files in `apps/llm_ranking/{models,services,views,urls}.py`, `frontend/src/pages/LLMRankingPage.vue`, `frontend/src/api/llm_ranking.js`, plus an untracked migration `apps/llm_ranking/migrations/0009_add_audit_logs.py`. | A large block of LLM ranking work (~330 lines across 6 files, including the `audit_logs` field) is **not even committed**, so it cannot be on prod. |
| No automated deploy | `DEPLOY.md` and `.github/workflows/ci.yml` (per docs) confirm CI only lints on push to main. Production updates require an operator to SSH to EC2 and run `bash scripts/deploy.sh`. | "Code in main" ≠ "code in prod". Even committed changes are not on prod until someone runs the deploy script. |

### What is actually on prod right now

- **Backend:** whatever commit was last pulled by `scripts/deploy.sh` on the EC2 host. From local history, the most recent commit on `main` is `11d0456`. Anything you see locally that is not yet committed (the `audit_logs`/live-pipeline-log work) is **definitely not deployed**.
- **Frontend:** the prod `frontend` container builds from source inside `docker/docker-compose.prod.yml` at deploy time, so a successful `scripts/deploy.sh` run would produce a fresh bundle. Your **local** `frontend/dist/` is stale, but that does not affect prod — prod doesn't ship your local dist/.
- **Net effect:** prod is, at best, one deploy behind your local main, and definitely behind your uncommitted work.

### How to verify against the live host

```bash
ssh -i cansee-deploy.pem ubuntu@<cansee-ec2-ip>
cd /opt/cansee/ftb-api-26
git rev-parse HEAD                                    # what commit is deployed
docker compose -f docker/docker-compose.prod.yml ps   # which services are up
docker compose -f docker/docker-compose.prod.yml logs --tail=50 web
docker compose -f docker/docker-compose.prod.yml logs --tail=50 frontend
curl -I https://cansee.ai/health/
```

If `git rev-parse HEAD` on the host is older than `11d0456`, the backend is behind. If you want the latest local work on prod, you must:
1. Commit the dirty files (including the new migration `0009_add_audit_logs.py`).
2. Push to `origin/main`.
3. SSH to the host and run `bash scripts/deploy.sh`.

### Why "the UI shows no reactions to what is happening"

The branch you have locally is the one that adds live audit progress (the `audit_logs` JSON field on `LLMRankingAudit` and the `/audits/<aid>/logs/` endpoint that the UI polls to show "Generated 8 prompts", "Claude query succeeded", etc.). That is precisely the work that is **uncommitted on disk and not on prod**. So the deployed UI has no live feedback because the backend it talks to does not yet expose live progress, and the frontend bundle on prod does not poll for it. Ship the pending changes and the reactions show up.

---

## 2. System overview

Cansee is a Django + Vue SaaS that measures and improves how a brand appears in answers from large language models (Claude, GPT-4o, Gemini, Perplexity). The flagship feature is the **LLM Ranking Audit**: given a business profile, the system asks several LLMs the kinds of questions a buyer would ask, parses the answers, and scores brand visibility, citation share, and competitive ranking over time.

```
┌────────────────────────────────────────────────────────────────────┐
│                         Cloudflare (Full Strict TLS)               │
└────────────────────────────────────────────────────────────────────┘
                                  │
┌────────────────────────────────────────────────────────────────────┐
│                       EC2 (Ubuntu 22.04, t3.small)                 │
│                                                                    │
│   nginx ─┬──> /api/*  ──> web (Django + Gunicorn, ASGI)            │
│          └──> /       ──> frontend (Vue 3 build artifact)          │
│                                                                    │
│   web ──┬──> Postgres 16  (audits, results, usage, accounts)       │
│         ├──> Redis        (cache + Celery broker)                  │
│         └──> Celery worker + beat (queue: ai, default, …)          │
│                                          │                         │
│                                          ▼                         │
│   Celery worker ──> Anthropic / OpenAI / Google / Perplexity APIs  │
└────────────────────────────────────────────────────────────────────┘
```

8 containers: `db`, `redis`, `web`, `celery` (worker + beat in one), `frontend` (build artifact), `nginx`, and the internal `intelligence` and `sources` services (section 8).

---

## 3. Frontend

- **Stack:** Vue 3 (composition API), Pinia, Vue Router 4, Tailwind 4, Vite 7, axios, Chart.js + vue-chartjs.
- **Layout:** `frontend/src/{api,pages,components,stores,router,layouts,composables,constants}/`.
- **HTTP client:** `frontend/src/api/client.js` — single axios instance, baseURL `/api/v1`, request interceptor injects Bearer token + `X-Request-ID`, response interceptor unwraps `{success, data}` envelope, performs silent token refresh on 401 with a queued-request retry, and exponentially backs off on 429.
- **LLM ranking UI:** `frontend/src/pages/LLMRankingPage.vue` (≈196 KB; the heaviest screen). Renders the Brand Overview dashboard: KPI cards (visibility %, citation share, brand rank, closest competitor), provider/time/topic filters, competitor visibility line chart, competitor rankings table, citation share trend, top sources, and a per-model usage breakdown.
- **Realtime:** there is no WebSocket layer; `config/asgi.py` serves plain HTTP. The LLM ranking audit pipeline uses REST polling against `/audits/<aid>/logs/`.
- **No "thinking"/reasoning UI:** the frontend renders final responses, mention extraction, sentiment, and per-call token cost. It does not display extended thinking traces.

---

## 4. Backend — LLM ranking pipeline

### Apps relevant to the LLM product
- `apps/llm_ranking/` — generative-engine-optimization (GEO) audits.
- `apps/voice_agent/` — separate voice conversation product.
- `apps/agents/` — REMOVED 2026-08-24 (hired-agents feature deleted end to end: app, API, UI, celery schedule; tables dropped via `migrate agents zero`).
- `apps/messaging/`, `apps/leads/`, `apps/social_leads/`, `apps/competitors/` — call into the same `core.ai_tracking` so all LLM spend lands in one ledger.

### Data model — `apps/llm_ranking/models.py`
- **`LLMRankingAudit`** — one audit run.
  - Inputs: `business_name`, `industry`, `location`, `keywords`, `description`, `context_urls`.
  - Status: `pending → running → completed | failed`.
  - Aggregates: `overall_score` (0-100), `mention_rate`, `avg_mention_rank`, Wilson 95 % CIs.
  - Progress: `queries_completed / total_queries`, `started_at`, `completed_at`, `duration_seconds`.
  - **`audit_logs`** (JSON, the new field in the uncommitted migration `0009_add_audit_logs.py`): `[{"ts": ISO8601, "level": "info|warn|success|error", "msg": str}]`. The UI polls this for live progress.
  - `extraction_method` (heuristic vs LLM-based), `frequency` for periodic audits, snapshotted business context.
- **`LLMRankingResult`** — one (prompt × provider) query.
  - `provider ∈ {claude, gpt4, gemini, perplexity, meta_llama, mistral, cohere, deepseek, grok, amazon_nova}`.
  - `prompt`, `response_text`, `is_mentioned`, `mention_rank`, `sentiment`, `confidence_score`, `mention_context`, `competitors_mentioned`, `citations`, `primary_recommendation`, `extraction_model`, `extraction_version`, `run_id`.
- **`LLMRankingSchedule`** — cron-style configuration: providers, frequency, `next_run_at`, `last_run_at`.

### Service layer — `apps/llm_ranking/services/ranking_service.py`
`LLMRankingService`:
1. **Prompts** — an audit runs exactly the prompts the user saved; nothing is generated.
2. **Per-provider query methods**, each returning `(succeeded, response_text, error_message)`:
   - `_query_claude()` → `anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)`, model `claude-sonnet-4-20250514`, system prompt enforces numbered lists for tool listings (so rank extraction is deterministic).
   - `_query_openai()` → `openai.OpenAI()`, model `gpt-4o-mini`.
   - `_query_gemini()` → `google.generativeai`, model `gemini-1.5-flash`.
   - `_query_perplexity()` → REST `api.perplexity.ai/chat/completions`, model `llama-3.1-sonar-small-128k-online`.
   - Meta / Mistral / Cohere / DeepSeek / Grok / Amazon Nova are wired in the schema/UI but not yet implemented in the service.
3. **`_analyze_mention()`** — given a response, detects whether the brand was mentioned (name terms + keyword terms), regex-extracts the rank from numbered lists, classifies sentiment heuristically, returns `(is_mentioned, mention_rank, sentiment, confidence_score, mention_context)`.
4. Every provider call records token usage via `core.ai_tracking.record_usage(...)`.

### Async orchestration — `apps/llm_ranking/tasks.py` and `config/celery.py`
- **`run_llm_ranking_audit(audit_id)`** — Celery task on the `ai` queue. `max_retries=1`, `countdown=30 s` on failure, sets audit `FAILED` + `error_message` on exception.
- **`dispatch_scheduled_audits()`** — Celery beat task, runs every 15 minutes, finds enabled schedules with `next_run_at <= now`, creates the audit, enqueues `run_llm_ranking_audit.delay(...)`, and advances `next_run_at` by the configured frequency.
- **Worker config** (`docker/docker-compose.prod.yml` `celery` service):
  ```
  celery -A config.celery worker --loglevel=info --concurrency=2 \
    --queues=default,high,low,ai,integrations,webhooks
  celery -A config.celery beat --loglevel=info \
    --scheduler=django_celery_beat.schedulers:DatabaseScheduler
  ```

### REST API — `apps/llm_ranking/api/v1/urls.py`
| Method | Path (under `/api/v1/llm-ranking/<wid>/`) | View | Purpose |
|---|---|---|---|
| GET / POST | `audits/` | `LLMRankingAuditListView` | List / create (queues Celery task) |
| GET | `audits/<aid>/` | `LLMRankingAuditDetailView` | Full audit + results |
| POST | `audits/<aid>/run/` | `LLMRankingAuditRunView` | Manual trigger (used in dev with `CELERY_TASK_ALWAYS_EAGER`) |
| GET | `audits/<aid>/logs/` | `LLMRankingAuditLogsView` | **Live pipeline log polling** (the new endpoint) |
| GET | `audits/<aid>/breakdown/` | `LLMRankingProviderBreakdownView` | Per-provider mention stats |
| GET | `audits/<aid>/recommendations/` | `LLMRankingRecommendationsView` | Actionable suggestions |
| GET | `audits/<aid>/prompts/` | `LLMRankingPromptResultsView` | Filterable prompt-level results |
| GET | `audits/<aid>/providers/<provider>/` | `LLMRankingProviderDetailView` | Per-provider deep-dive |
| GET | `usage/` | `LLMRankingUsageView` | Token / cost metering |
| GET | `history/` | `LLMRankingHistoryView` | Trend over time |
| GET / POST / DELETE | `schedule/` | `LLMRankingScheduleView` | Manage periodic audits |

All views inherit from `core.views.base.TenantScopedAPIView`, which enforces `IsAuthenticated` and calls `WebsiteService.get_for_user()` (raises 403 if the caller doesn't own the website) so multi-tenant isolation is centralized.

### Live progress flow (the bit the UI is missing in prod)
```
UI ──poll──> GET /audits/<aid>/logs/?since=<ts>
              │
              ▼
     LLMRankingAuditLogsView returns audit.audit_logs JSON
              ▲
              │ append
   Celery task (run_llm_ranking_audit)
     ├─ "Starting audit for Cansee"           (info)
     ├─ "Generated 8 prompts"                   (info)
     ├─ "Claude query 1/8 succeeded (842ms)"    (success)
     ├─ "OpenAI query 2/8 failed: rate limit"   (warn)
     └─ "Audit completed: score 73"             (success)
```
Once the migration is applied and the new code is deployed, the UI gets the live "what is happening" feedback you described as missing.

---

## 5. Which LLMs we prompt, and what we measure

### Models actively called from code today
| Provider | Model | Where |
|---|---|---|
| Anthropic | `claude-sonnet-4-20250514` | Audit responses + prompt generation, `services/ranking_service.py` |
| OpenAI | `gpt-4o-mini` | Audit responses |
| Google | `gemini-1.5-flash` | Audit responses |
| Perplexity | `llama-3.1-sonar-small-128k-online` | Audit responses (web-grounded) |

The DB schema and UI also enumerate Meta Llama, Mistral, Cohere, DeepSeek, Grok, and Amazon Nova; these are placeholders pending implementation in the service layer.

### Metrics — `core/ai_tracking.py`
Every LLM call funnels through `record_usage(...)`, which writes one row to `AITokenUsage` capturing:

- `module` (`lead_finder`, `messaging`, `llm_ranking`, `seo_keywords`, `analytics`, …)
- `provider` (`anthropic`, `openai`, `google`)
- `model_name`
- `input_tokens`, `output_tokens` → `total_tokens`
- `duration_ms` (per-call latency)
- `estimated_cost_usd` — computed from a hard-coded price table:
  - `claude-sonnet-4-20250514`: **$3 / $15** per 1 M (input / output)
  - `claude-haiku-4-5-20251001`: **$0.80 / $4** per 1 M
  - `claude-3-5-sonnet-20241022`: **$3 / $15** per 1 M
  - default fallback: $3 / $15
- `user`, `website`, free-form `metadata`

`get_usage_summary()` aggregates the table into:
- totals (calls, input/output/total tokens, cost USD)
- per-module breakdown
- per-model breakdown
- daily trend

### Per-audit ranking metrics (from `LLMRankingAudit` and `LLMRankingResult`)
- **`overall_score`** (0-100) — composite GEO score.
- **`mention_rate`** — share of prompts where the brand was named, with Wilson 95 % CI (`mention_rate_ci_lower / _upper`) for statistical honesty given small `n`.
- **`avg_mention_rank`** — average position when mentioned (lower is better).
- **Per-result fields:** `is_mentioned`, `mention_rank`, `sentiment`, `confidence_score`, `mention_context`, `is_linked` (hyperlinked vs plain mention), `competitors_mentioned` (list with their position + linked flag), `primary_recommendation`, `citations` (URL list).
- **Provenance:** `extraction_model`, `extraction_version`, `run_id` (replicate index for multi-run audits) so historical scores remain comparable when the extraction logic changes.

---

## 6. End-to-end request lifecycle (LLM audit)

1. **User clicks "Run audit"** in `LLMRankingPage.vue`.
2. **POST `/api/v1/llm-ranking/<wid>/audits/`** → `LLMRankingAuditListView` validates with `RunAuditSerializer`, falls back to the website's stored fields when inputs are omitted, auto-detects configured providers, persists the `LLMRankingAudit` row, and returns **202 Accepted** with the lightweight serializer.
3. **Celery enqueue:** unless `CELERY_TASK_ALWAYS_EAGER`, the view calls `run_llm_ranking_audit.delay(audit_id=str(audit.id))` onto the `ai` queue.
4. **Worker (`celery` container)** runs `LLMRankingService.run_audit(audit_id)`:
   1. Sets status `running`, snapshots business context, appends `"Starting audit"` to `audit_logs`.
   2. `generate_prompts()` calls Claude to produce variants (recorded to `AITokenUsage`).
   3. For each `(prompt, provider)`:
      - Query the provider, capture latency, record token usage.
      - `_analyze_mention()` extracts rank, sentiment, competitors, citations.
      - Persist a `LLMRankingResult`.
      - Append a log line; bump `queries_completed`.
   4. Compute aggregates (mention rate + Wilson CI, avg rank, overall score).
   5. Status `completed`, set `completed_at` / `duration_seconds`, append final log line.
5. **Frontend poll loop:** while the audit is `running`, the UI polls `/audits/<aid>/logs/` for new log lines and `/audits/<aid>/` for live aggregates, then renders the dashboard from `breakdown/`, `recommendations/`, `prompts/`, and per-provider endpoints once `completed`.

---

## 7. Deployment topology (recap)

- **Image build:** all containers built on the host from the checked-out source by `docker compose -f docker/docker-compose.prod.yml up -d --build` inside `scripts/deploy.sh`. There is no registry; the host is the build farm.
- **Resource limits:** db 300 M, redis 150 M, frontend (init) build, web 500 M, celery 400 M, nginx 50 M, intelligence 150 M, sources 150 M. Limits total ~1.7 GB on a 2 GB t3.small; a 2 GB swapfile is assumed but NOT provisioned by the script -- `deploy.sh` preflight warns when no swap is active and prints the commands to add one.
- **DB migrations:** `python manage.py migrate --noinput` runs at the end of `scripts/deploy.sh`. The new `apps/llm_ranking/migrations/0009_add_audit_logs.py` will be applied the next time deploy runs **after** it is committed and pushed.
- **Frontend bundle:** the `frontend` service rebuilds via Vite at deploy time, so the `dist/` you see locally is irrelevant to prod — what matters is the source on the host at deploy time.
- **No CI/CD deploy:** GitHub Actions only lints. Deploys are operator-driven SSH + script.
- **No blue/green:** `docker compose down` then `up` produces a brief outage on every release.

---

## 8. Internal services (`services/`)

Commonly used capability logic runs as owned downstream services, each in
its own container with a unique internal FQDN:

| Service | Internal FQDN | Owns |
|---|---|---|
| `intelligence` | `http://intelligence.ftb.internal:8000` | LLM brand/sentiment/issue extraction, SERP relevance gate |
| `sources` | `http://sources.ftb.internal:8000` | Perplexity web search, Reddit/Yelp/page content readers (SSRF-guarded) |

Pattern:

- **Network:** compose default bridge only. The FQDNs are compose network
  aliases; there are no published ports, no nginx routes, and no public
  DNS. A browser can never reach these services -- only `web` and
  `celery` can, over the Docker network.
- **Auth:** static per-service bearer token (`INTELLIGENCE_AUTH_TOKEN`,
  `SOURCES_AUTH_TOKEN`) from `.env.prod`, injected into both the service
  and its Django callers. A
  service with an empty token refuses requests (503) rather than failing
  open.
- **Stateless services, stateful facades:** quotas (`core.quota`),
  circuit breakers, and `AITokenUsage` cost recording stay in the Django
  facade modules (`apps/citations/services/{source_sentiment,web_search,
  content_reader}.py`). The services return usage numbers; the facades
  record them.
- **In-process fallback (single source of truth):** the pure logic lives
  in `services/*/logic.py`, importable without FastAPI or Django. When
  `INTELLIGENCE_SERVICE_URL` / `SOURCES_SERVICE_URL` are unset (dev,
  tests, CI), the facades call that logic in-process with identical
  behavior. Production sets the URLs in `.env.prod` to switch to HTTP.
  Rollback is therefore instant: blank the URL vars and restart
  web/celery.
- **Fail-soft:** a down service degrades exactly like a missing API key
  today -- single scan rows get error markers, the relevance gate fails
  open, searches return empty. It never breaks request handling.
- **SSRF note:** `services/sources/{url_safety,safe_http}.py` are
  Django-free copies of `core/validators/*` (which other apps still
  import and which raise Django `ValidationError`). Keep them in sync;
  `services/sources/tests/test_sources_ssrf.py` mirrors the guard tests.

---


## 9. Action items implied by the current state

1. **Commit the dirty tree** (don't lose the audit_logs work and the new migration `0009_add_audit_logs.py`).
2. **Push to `origin/main`** and let CI lint pass.
3. **SSH to the EC2 host and run `bash scripts/deploy.sh`** so the backend gets the new endpoint + migration and the frontend container rebuilds with the live-progress UI.
4. **Verify on prod:**
   - `docker compose -f docker/docker-compose.prod.yml exec web python manage.py showmigrations llm_ranking` — confirm `0009_add_audit_logs` is applied.
   - Trigger a new audit from the UI and watch the activity feed populate.
   - `curl -I https://cansee.ai/health/` returns 200.
5. **Optional cleanup unrelated to this report:** Meta / Mistral / Cohere / DeepSeek / Grok / Amazon Nova are advertised in the model dropdown but not implemented in `ranking_service.py`. Either ship the integrations or hide the options until they exist.
