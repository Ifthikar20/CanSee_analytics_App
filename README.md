# Cansee API

Backend for [cansee.ai](https://cansee.ai). For each prompt a customer tracks, Cansee
compares where their site ranks on Google with where each AI engine (ChatGPT, Claude,
Gemini, Grok, Perplexity) places the brand, and shows the gap.

This repository is the API only. The Vue UI lives in its own repository and ships to
production as the image `ghcr.io/ifthikar20/cansee-ui`.

## Stack

Python 3.12, Django 5.1, Django REST Framework, PostgreSQL 16, Redis, Celery 5.4.

Two internal FastAPI services live under `services/` (`intelligence`, `sources`). When
their `*_SERVICE_URL` variables are unset, Django runs the same code in-process.

## Layout

| Path | What it holds |
|---|---|
| `apps/llm_ranking` | Prompt runs across the AI engines: answers, mentions, scores |
| `apps/search_gap` | Google rank per tracked prompt (SerpAPI or ScraperAPI) next to AI placement |
| `apps/agent` | The agent workspace: chats, SEO/GEO research tools, writing agents, reports (see Agent service) |
| `apps/prompt_library` | Tracked prompts, industries, prompt suggestions |
| `apps/citations` | Sources the AI answers cite |
| `apps/brand_vault` | Brand facts and brand checks |
| `apps/rag`, `apps/assistant` | Per-website knowledge base and the assistant API |
| `apps/onboarding` | First-run site scan |
| `apps/search_console`, `apps/web_analytics` | Search Console, GA4 and Cloudflare connections |
| `apps/analytics` | First-party pixel (`/p.js`) ingestion and its reports |
| `apps/notifications` | In-app notifications; Slack, Discord, email and SMS delivery |
| `apps/billing`, `apps/metering` | Polar subscriptions and AI usage metering |
| `apps/accounts`, `apps/websites` | Users, organizations, sign-in; projects (tracked sites) |
| `apps/content_studio`, `apps/leads` | Retired; models and migrations kept so the tables stay valid |
| `core/` | Middleware, DRF interceptors, permissions, field encryption, LLM base class |
| `config/` | Settings, URLs, Celery app and beat schedule |
| `docker/` | Dockerfiles, compose files, nginx configs, the served pixel JS |
| `scripts/` | Deploy, backup, user and log helpers (run from a laptop or the host) |

## Local setup

The quickest way: with Docker Desktop running and `Cansee_analytics_UI_APP` cloned next
to this repo, run

```bash
scripts/run-local.sh --user you@example.com "Your Name"
```

It creates a minimal `.env` if there is none, builds and starts Postgres, Redis and the
API, waits for it, creates the login (asking for a password), and starts the UI at
http://localhost:5173/login. Later runs need no `--user`. `scripts/run-local.sh stop`
stops the containers and keeps the data; `--help` lists the other commands. The steps
below do the same by hand.

1. Create `.env` in the repo root. The dev settings need only a few values:

   ```bash
   DEBUG=True
   DJANGO_SECRET_KEY=<any long random string>
   FIELD_ENCRYPTION_KEY=<output of: make generate-key>
   PUBLIC_SCHEME=http
   ```

   Add provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SERPAPI_KEY`, ...) to get
   real results. Every variable is listed, with a one-line note, in `.env.prod.example`.

2. Start Postgres, Redis and the API (it runs migrations, then `runserver` on :8000):

   ```bash
   docker compose -f docker/docker-compose.dev.yml -f docker/docker-compose.dev.override.yml up -d db redis web
   ```

   Dev settings run Celery tasks inline, so no worker is needed for most flows. Add
   `celery celery-beat` to the command to run the worker and the periodic schedule.

3. Create an account (public sign-up is off by default):

   ```bash
   docker compose -f docker/docker-compose.dev.yml -f docker/docker-compose.dev.override.yml \
     exec web python manage.py create_user you@example.com "Your Name"
   ```

## Creating users

`manage.py create_user <email> "<full name>" [--org SLUG] [--password-env VAR]` creates an
account with a verified email. The password is typed at a hidden prompt, or read from
the environment variable named by `--password-env`; it is never a command-line argument.
`--org` adds the user to an existing organization as a member.

`scripts/create_user.sh` wraps the command for local use or for production over SSH.

## Tests and lint

```bash
ruff check .   # the gate CI runs (.github/workflows/ci.yml)
pytest         # needs Postgres on localhost:5432, user postgres/postgres (config/settings/test.py)
```

The pytest suite lives in https://github.com/Ifthikar20/CanSee_analytics_Test under `backend/`
(`tests/` is gitignored here); its `scripts/run-backend-tests.sh` mirrors it into this
checkout and runs it. It is not run in CI.

With `DEBUG=True` the OpenAPI schema is served at `/api/schema/`, with Swagger UI at
`/api/schema/swagger/` and ReDoc at `/api/schema/redoc/`. They are not served in production.

## Conventions

- Tenant-scoped endpoints extend `TenantScopedAPIView` (`core/views/base.py`) and call
  `self.get_website()`. It resolves access through `WebsiteService.get_for_user()`, which
  answers 404 for both missing and foreign ids.
- LLM calls go through a provider class built on `core/llm/base.py`, which records token
  usage and cost for every call.

## Plans and prompt runs

The Free plan is metered in **prompt runs**: one prompt answered by one AI engine is one
run. A run counts only when the engine gives a successful, non-empty answer; a failure,
a refusal, an empty answer or an engine that was never asked counts nothing. The runs
are shared by every project and engine of the account (the project owner's account, or
the organization's while organization features are on) and renew with the billing
period. Every place that asks an engine counts: audits and scans, Check now, scheduled
prompt checks, country checks, Ask AI, Autopilot, the agent's engine checks and research
agents, the brand research engine lane, shopping checks and the prompt smoke test.
Paid plans are unlimited here (their runs are still recorded). The ledger is
`apps.metering` (`PromptRun`, `UsageCounter`); the service is
`apps/metering/services/prompt_runs.py`. `GET /api/v1/billing/prompt-runs/` and the
session's `limits.prompt_runs` report used and remaining runs.

| Setting | Default | What it does |
| --- | --- | --- |
| `FREE_MONTHLY_PROMPT_RUNS` | `20` | Prompt runs the Free plan gets each billing period. |
| `PROMPT_RUN_STALE_MINUTES` | `30` | A run still reserved this long after it was taken (its worker died mid-call) is given back by `apps.metering.tasks.release_stale_prompt_runs`, which Celery beat runs every 15 minutes. |
| `AI_FREE_MONTHLY_CAP_USD` | `3.0` | Hidden AI spend backstop for the Free plan's Claude-only work (the agent, extraction, reports, onboarding). Free accounts never see it in dollars; its denial says the free AI allowance is used up. |

## Agent service

The agent workspace (`apps/agent`, mounted at `/api/v1/agent/`) is a chat agent on
Claude Opus 5.5 that researches SEO and GEO visibility, compares the two and hands
writing to nine writers: landing pages, AEO articles, ad campaigns, page revisions,
llms.txt files, comparison pages, social posts, PR (press releases, media pitches,
correction requests) and messaging briefs. For broad research it runs the research
agents (`research_swarm`): up to `AGENT_SWARM_MAX_TASKS` agents in parallel, each
fetching one Google results page, asking one AI engine one prompt, reading one page or
analysing one prompt. It ships switched off.

**Two processes.** Gunicorn serves the ordinary endpoints (chats, artifacts, reports,
brand context, suggestions, activity, status). Everything under `/api/v1/agent/live/`
(starting and cancelling runs, the event stream and long-poll, page browsing) is served
by a separate `agent` service: the web image under uvicorn, one worker, settings
`config.settings.agent` (`AGENT_PROCESS=True`, `CONN_MAX_AGE=0`), ASGI app
`config.agent_asgi` (plain Django; it starts the run heartbeat at boot, so `status/`
reports the service as live from the start). Runs execute on a
thread pool inside that process. nginx routes the `live/` prefix there with buffering
off and answers `503 agent_unavailable` (JSON) when the service is down or not
enabled, and `503 maintenance` (JSON) while the maintenance switch is on. One client
(`CF-Connecting-IP` behind Cloudflare, else the peer address) holds at most 20
connections there; past that nginx answers `429 too_many_streams` (JSON). A gunicorn
worker that receives a `live/` request answers `503 agent_unavailable`.

**Switches.** `AGENT_ENABLED` (default off) gates every agent endpoint except
`status/`, which reports what is configured: the switch, the agent process, the model,
which AI engines and Google results provider have keys, the user's AI allowance and prompt
runs, whether the research agents can run (`research`) and a `setup` block of plain-language
issues (setting names such as `OPENAI_API_KEY` appear only for staff and while `DEBUG` is
on). Without
`ANTHROPIC_API_KEY` no run starts (`503 agent_not_configured`); without `SERPAPI_KEY`
or `SCRAPERAPI_KEY`
Google research reports `serp_not_configured`; an engine without a key is reported as
not configured. Nothing is filled in with sample data. The `AGENT_*` settings in
`config/settings/base.py` (model, effort, token and cost ceilings, concurrency,
SerpAPI pages, browse limits, stream and heartbeat timing) are all env-overridable.

**Turning it on (owner steps).**
1. Size the host: a t3.medium is recommended (the 2 GB t3.small is already full), and
   add a 2 GB swapfile (`scripts/deploy.sh` prints the commands when swap is missing).
2. In `docker/.env` on the host: `COMPOSE_PROFILES=agent`.
3. In `.env.prod`: `AGENT_ENABLED=true` and `ANTHROPIC_API_KEY`.
4. Check that Cloudflare caches nothing under `/api/` (event streams must pass through).
5. Deploy. `scripts/deploy.sh` detects the profile, recreates the agent after
   migrations (in-flight runs end as `interrupted`), and restarts nginx whenever the
   running container still holds an older `nginx.conf` than the checkout.
6. After the first deploy, check the routing:
   `docker compose -f docker/docker-compose.prod.yml exec nginx nginx -T | grep agent/live`.

**Turning it off.**
1. In `docker/.env` on the host: remove `agent` from `COMPOSE_PROFILES`.
2. In `.env.prod`: `AGENT_ENABLED=false`.
3. Deploy. `scripts/deploy.sh` removes the agent container when the profile is off
   (runs in flight end as `interrupted`); nginx then answers the `live/` routes with
   `503 agent_unavailable` and `status/` reports the agent as disabled. Without a
   deploy, remove it by hand:
   `docker compose -f docker/docker-compose.prod.yml --profile agent rm -s -f agent`.

Celery beat runs `apps.agent.tasks.sweep_stale_agent_runs` every minute, so a run
whose process died (deploy, crash) ends as `interrupted` instead of staying "running";
the agent process also ends such a run as soon as a reader asks for it. A daily
`apps.agent.tasks.purge_expired_agent_chats` deletes chats idle for longer than
`AGENT_CHAT_RETENTION_DAYS` (0, the default, keeps every chat; artifacts and reports
made in a deleted chat stay).

In dev, `runserver` is the agent process (`config/settings/dev.py`; `AGENT_ENABLED` and
`AGENT_PROCESS` default to true there). `docker/docker-compose.yml` runs gunicorn's several workers
instead, which would interrupt each other's runs, so it sets `AGENT_PROCESS=false` and
has no agent process; use `docker/docker-compose.dev.yml` to work on the agent. The
test suite runs turns inline against a fake Anthropic client and never calls the real
API.

**Running the agent locally.** `scripts/run-local.sh` starts the dev stack
(`docker/docker-compose.dev.yml`), where the agent is already on. Uncomment and fill in
the keys in the `.env` it wrote, then run the script again (it recreates the `web`
container with the new values):

| Key | Needed for |
| --- | --- |
| `ANTHROPIC_API_KEY` | Required. Chats, the writers and the research agents' analyses. The key needs access to `AGENT_MODEL` (`claude-opus-5-5`) and `AGENT_WORKER_MODEL` (`claude-haiku-5-5`). |
| `SERPAPI_KEY` or `SCRAPERAPI_KEY` | Optional. Google results in research; without one, research measures the AI engines only. |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `XAI_API_KEY` | Optional. The AI engines research asks (Claude uses `ANTHROPIC_API_KEY`). An engine without a key is skipped and reported as not set up. |
| `AI_FREE_MONTHLY_CAP_USD` | Optional. An account without a subscription has a small monthly AI allowance, which an Opus reply or two can use up; raise it for local testing, or set the test user's `monthly_ai_cost_cap_usd`. |

`AGENT_ENABLED=false` in `.env` turns the agent off in dev. `GET /api/v1/agent/status/`
shows what is configured.

**How many agents run at once.** All of these are env settings (defaults in
`config/settings/base.py`, commented overrides in `.env.prod.example`):

| Setting | Default | Limits |
| --- | --- | --- |
| `AGENT_MAX_CONCURRENT_RUNS` | `6` | Replies the agent process runs at once (its run threads). A reply sent while all are busy waits in line: its run reports `queue` and `run.queued` events (below). |
| `AGENT_USER_ACTIVE_RUNS` | `2` | Replies one user may run at once across chats (a chat always runs one at a time). More spends the allowance faster; the allowance and the per-reply cost cap still stop every reply. |
| `AGENT_SWARM_MAX_TASKS` | `50` | The one ceiling on research agents per research. The tool refuses larger plans, and the orchestrator's prompt and tool description state the number (new chats pick up a new value; older chats keep theirs). 50 fits 5 prompts with all five engines. |
| `AGENT_SWARM_WORKERS` | `8` | Research agents running at once in one research. |
| `AGENT_SWARM_PROCESS_WORKERS` | `16` | Research agents running at once across the process. A task that timed out keeps its slot until its thread returns. |
| `AGENT_SWARM_SERP_CONCURRENCY`, `_BROWSE_`, `_ENGINE_`, `_HAIKU_` | `5`, `6`, `3`, `8` | Per-provider limits shared by every research (`ENGINE` is per engine). Keep them within the SerpAPI or ScraperAPI plan's concurrency and each engine's rate limits, or agents fail with 429s. Raising the worker counts without these only queues tasks. |

Cost notes: a full research at 50 agents can use about $0.89 of AI allowance (25
engine answers at $0.02, 35 analyses at $0.004, the report at $0.25), plus 5 to 10
Google results pages that the platform pays for (`SERPAPI_COST_PER_SEARCH_USD`) and
no allowance counts. A reply's cost (`cost_usd` on the run and message, `run.usage`)
includes the AI engines' answers it asked for, and a reply stops at
`AGENT_RUN_MAX_COST_USD` or `AGENT_RUN_MAX_SECONDS` during engine checks and research
too. A run still going `AGENT_RUN_MAX_SECONDS` plus a minute after it started is
stopped by the heartbeat and fails with `run_timeout`; if its thread does not
answer, the heartbeat lets it go stale and the sweeper ends it as `interrupted`. More
threads use more memory: size the host before raising these (see below).

**Waiting for a free slot.** While a run waits for a run thread, `queue` on the run
(the `live/` start responses, the chat's `active_run` and the long-poll response) is
`{"position": n, "ahead": n - 1, "slots": AGENT_MAX_CONCURRENT_RUNS}`, and its event
stream carries `run.queued` with the same fields each time its place changes; `queue`
is `null` once a thread has it (then `run.started` follows) or when the process cannot
tell (a gunicorn worker).

## Deploy

A push or merge to `main` deploys to production: `.github/workflows/deploy.yml` waits for
CI lint, then runs `scripts/deploy.sh` against the EC2 host. The deploy also pulls the
latest UI image. Production settings come from `.env.prod` on the host, created from
`.env.prod.example`. See `DEPLOY.md` for the release flow, secrets and rollback.
