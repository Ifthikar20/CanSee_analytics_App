# Deployment Guide

Cansee (cansee.ai) is deployed to a single EC2 host running Docker
Compose. **Pushing or merging to `main` auto-deploys to production**
via `.github/workflows/deploy.yml`, which waits for the CI lint job to
pass and then runs `scripts/deploy.sh` against the EC2 box.

If you need to deploy manually (e.g., off-main hotfix, debugging CI),
the same script is runnable from any machine that has the PEM key —
see "Manual deploy" below.

## Architecture

- **Host:** Ubuntu 22.04 EC2 instance (sized around t3.small; a 2 GB swapfile
  is recommended -- `deploy.sh` checks and warns if none is active but does
  not create one).
- **App directory on host:** `/opt/cansee/ftb-api-26`
- **Orchestration:** `docker/docker-compose.prod.yml`
- **Services:** `db` (Postgres 16), `redis`, `web` (Django/Gunicorn),
  `celery` (worker + beat in one container), `frontend` (Vue build artifact
  from `ghcr.io/ifthikar20/cansee-ui`), `nginx` (TLS termination + static),
  `intelligence` and `sources` (internal FastAPI sidecars, rebuilt by the
  deploy build step; see docs/ARCHITECTURE.md "Internal services").
- **Edge:** Cloudflare (SSL: Full Strict) → nginx container on ports 80/443.
  TLS certs are mounted from `/opt/cansee/ssl` on the host.
- **Env file:** `/opt/cansee/ftb-api-26/.env.prod` (never committed; created
  from `.env.prod.example` on first deploy).

## Release flow (automatic)

1. Open a PR against `main`. CI runs the lint job.
2. Merge to `main` once approved.
3. `.github/workflows/deploy.yml` triggers automatically:
   - Waits for the `Lint` check on the same SHA to pass.
   - SSHes to the EC2 host using the `EC2_SSH_KEY` repository secret.
   - Runs `bash scripts/deploy.sh` against prod, which performs:
     - `git pull origin main` in `/opt/cansee/ftb-api-26`
     - Verifies `.env.prod` exists
     - `docker compose ... up -d --build`
     - `python manage.py migrate --noinput`
     - `python manage.py collectstatic --noinput`
     - Hits `http://localhost/health/` as a smoke check.
   - Hits `https://cansee.ai/health/` from the GitHub runner as a
     final external smoke test.

Watch the deploy in the Actions tab of the GitHub repo. A failed
deploy leaves prod on the previous image (Docker only swaps containers
in once the new image builds successfully).

## Required GitHub repository secrets

Set under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `EC2_SSH_KEY` | The contents of `fynda-deploy.pem` (the full PEM body, including `-----BEGIN` / `-----END` lines). |
| `EC2_HOST` (optional) | EC2 IP or DNS. Defaults to `18.208.3.3`. |
| `EC2_USER` (optional) | SSH user. Defaults to `ubuntu`. |
| `REMOTE_DIR` (optional) | Repo path on host. Defaults to `/opt/cansee/ftb-api-26`. |

Test the SSH key once with `ssh -i fynda-deploy.pem ubuntu@<host> echo ok`
before pasting it into the secret.

## Manual deploy (fallback)

Triggers the workflow on demand from the GitHub UI:

> Actions → "Deploy to prod" → Run workflow → branch: `main`

Or run the script locally from your laptop (works from any machine
that has `fynda-deploy.pem` placed at the repo root):

```bash
bash scripts/deploy.sh
```

`scripts/deploy.sh` SSHes into the EC2 host itself; you do not need to
SSH in first.

## Verify after deploy

```bash
ssh -i ./fynda-deploy.pem ubuntu@18.208.3.3 \
  'cd /opt/cansee/ftb-api-26 && \
   docker compose -f docker/docker-compose.prod.yml ps'
curl -I https://cansee.ai/health/
```

## First-time server bootstrap

On a fresh EC2 box:

```bash
sudo mkdir -p /opt/cansee && sudo chown $USER:$USER /opt/cansee
git clone <repo-url> /opt/cansee
cd /opt/cansee/ftb-api-26
cp .env.prod.example .env.prod
nano .env.prod   # fill in secrets, DB creds, API keys
REPO_URL=<repo-url> bash scripts/deploy.sh
```

Then point the `cansee.ai` A record at the EC2 public IP and set Cloudflare
SSL mode to **Full (Strict)**.

## Common operations

All commands run from `/opt/cansee/ftb-api-26` on the host.

| Task | Command |
|------|---------|
| Tail web logs | `docker compose -f docker/docker-compose.prod.yml logs -f web` |
| Tail celery logs | `docker compose -f docker/docker-compose.prod.yml logs -f celery` |
| Restart web only | `docker compose -f docker/docker-compose.prod.yml restart web` |
| Run a migration manually | `docker compose -f docker/docker-compose.prod.yml exec web python manage.py migrate` |
| Django shell | `docker compose -f docker/docker-compose.prod.yml exec web python manage.py shell` |
| Create a user (prompts for the password) | `docker compose -f docker/docker-compose.prod.yml exec web python manage.py create_user <email> "<full name>"` |
| Postgres shell | `docker compose -f docker/docker-compose.prod.yml exec db psql -U postgres cansee` |
| Container status | `docker compose -f docker/docker-compose.prod.yml ps` |

## Rolling back

There is no built-in rollback. To revert:

```bash
cd /opt/cansee/ftb-api-26
git log --oneline -10
git checkout <previous-good-sha>
docker compose -f docker/docker-compose.prod.yml up -d --build
docker compose -f docker/docker-compose.prod.yml exec web python manage.py migrate
```

If the bad release included a forward migration that is not safely reversible,
restore the database from the most recent Postgres backup before rolling code
back.

## Things that are NOT automated

- CI does **not** run the test suite. The `Lint` job runs `ruff check .` only,
  and that is the sole gate the deploy workflow waits on. Run `pytest` locally
  before merging.
- There is no blue/green or canary. The deploy script takes the stack down
  with `docker compose down` before bringing it back up, so expect a brief
  outage on every release.
- Secrets rotation is manual: edit `.env.prod` on the host, then
  `docker compose -f docker/docker-compose.prod.yml up -d` to recreate
  affected containers.
- TLS certs in `/opt/cansee/ssl` are managed outside this repo.

## Paywall entitlement switch

The paywall (and all plan gating) is controlled by the `PAYWALL_ENABLED`
GitHub repository **variable** (Settings > Secrets and variables > Actions
> Variables), not by hand-edits on the server:

1. Set the variable to `True` (paywall on) or `False` (open app).
   Unset behaves as `False`.
2. Re-run the "Deploy to prod" workflow (or merge anything to `main`).
   The deploy pipeline syncs the value into `.env.prod` on the host and
   restarts the containers.

`scripts/deploy.sh` overwrites the `PAYWALL_ENABLED` line in the remote
`.env.prod` on every pipeline deploy, so a manual edit of that line on the
server survives only until the next deploy. Manual runs of the script
without `PAYWALL_ENABLED` in the environment leave the remote value
untouched.

With the paywall on, the gate is dismissible: after onboarding a user
lands on `/paywall` once and either starts the Pro trial or clicks
"Continue with the free plan", which records
`User.paywall_dismissed_at` (via `POST /api/v1/billing/paywall/dismiss/`)
so `next_route` returns `app` from then on. Clearing that field in the
Django admin re-arms the paywall for that user. Flipping the variable on
routes every existing unsubscribed user to the paywall once on their
next session; they self-serve out via the free plan or the trial, so no
grandfathering script is required.

## Pre-deploy checklist

- CI lint job is green on the commit being deployed.
- Local `pytest` and `ruff check .` pass.
- Any new env vars are added to `.env.prod` on the server **before** running
  the deploy script.
- New migrations have been reviewed for lock impact and reversibility.
- A recent Postgres backup exists.
