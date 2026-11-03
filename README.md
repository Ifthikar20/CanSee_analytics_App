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
| `apps/search_gap` | Google rank per tracked prompt (SerpAPI) next to AI placement |
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

## Deploy

A push or merge to `main` deploys to production: `.github/workflows/deploy.yml` waits for
CI lint, then runs `scripts/deploy.sh` against the EC2 host. The deploy also pulls the
latest UI image. Production settings come from `.env.prod` on the host, created from
`.env.prod.example`. See `DEPLOY.md` for the release flow, secrets and rollback.
