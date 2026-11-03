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

The pytest suite is kept local-only (`tests/` is gitignored) and is not run in CI.

With `DEBUG=True` the OpenAPI schema is served at `/api/schema/`, with Swagger UI at
`/api/schema/swagger/` and ReDoc at `/api/schema/redoc/`. They are not served in production.

## Conventions

- Tenant-scoped endpoints extend `TenantScopedAPIView` (`core/views/base.py`) and call
  `self.get_website()`. It resolves access through `WebsiteService.get_for_user()`, which
  answers 404 for both missing and foreign ids.
- LLM calls go through a provider class built on `core/llm/base.py`, which records token
  usage and cost for every call.

## Agent service

The agent workspace (`apps/agent`, mounted at `/api/v1/agent/`) is a chat agent on
Claude Opus 5.5 that researches SEO and GEO visibility, compares the two and drafts
landing pages, AEO articles and ad campaigns. It ships switched off.

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
which AI engines and Google results provider have keys, and the user's AI allowance. Without
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

In dev, `runserver` is the agent process (`config/settings/dev.py`; `AGENT_PROCESS`
defaults to true there). `docker/docker-compose.yml` runs gunicorn's several workers
instead, which would interrupt each other's runs, so it sets `AGENT_PROCESS=false` and
has no agent process; use `docker/docker-compose.dev.yml` to work on the agent. The
test suite runs turns inline against a fake Anthropic client and never calls the real
API.

## Deploy

A push or merge to `main` deploys to production: `.github/workflows/deploy.yml` waits for
CI lint, then runs `scripts/deploy.sh` against the EC2 host. The deploy also pulls the
latest UI image. Production settings come from `.env.prod` on the host, created from
`.env.prod.example`. See `DEPLOY.md` for the release flow, secrets and rollback.
