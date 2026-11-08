#!/usr/bin/env bash
#
# Run the whole Cansee app on this machine.
#
# Starts PostgreSQL, Redis and the Django backend in Docker (migrations run on
# start), waits until the backend answers, optionally creates a login, then
# starts the Vue UI dev server from the sibling Cansee_analytics_UI_APP folder.
#
# Usage:
#   scripts/run-local.sh                         start everything, UI in the foreground
#   scripts/run-local.sh --user EMAIL "NAME"     also create a login (asks for a password)
#   scripts/run-local.sh user EMAIL "NAME"       only create a login (backend must be up)
#   scripts/run-local.sh stop                    stop the containers (data is kept)
#   scripts/run-local.sh status                  show the containers
#   scripts/run-local.sh logs                    follow the backend log
#
# Options:
#   --workers     also start the Celery worker and beat. Beat runs the
#                 scheduled jobs, which spend API credits if keys are set.
#                 Not needed for scans: dev runs them inline.
#   --no-ui       start only the backend
#   --dry-run     print every command instead of running it
#
# Environment:
#   CANSEE_UI_DIR  path to the UI checkout (default: ../Cansee_analytics_UI_APP)
#
# Requires Docker Desktop (docker compose), Node.js with npm, and curl.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

UI_DIR="${CANSEE_UI_DIR:-$REPO_DIR/../Cansee_analytics_UI_APP}"
BACKEND_URL="http://localhost:8000"
UI_URL="http://localhost:5173"
# The override points the containers at each other by service name (db,
# redis), so .env can stay written for a native run.
COMPOSE=(docker compose -f docker/docker-compose.dev.yml -f docker/docker-compose.dev.override.yml)
WAIT_SECONDS=180

if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; N=$'\033[0m'
else
    B=''; G=''; Y=''; R=''; N=''
fi
say()  { echo "${G}==>${N} ${B}$*${N}"; }
warn() { echo "${Y}warning:${N} $*" >&2; }
die()  { echo "${R}error:${N} $*" >&2; exit 1; }

DRY_RUN=0
WITH_WORKERS=0
WITH_UI=1
USER_EMAIL=""
USER_NAME=""

run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '+'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

usage() { sed -n '3,27p' "$0" | sed 's/^# \{0,1\}//'; }

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        if [ "$DRY_RUN" = 1 ]; then warn "$1 not found (ignored in --dry-run)"; return; fi
        die "$1 not found. $2"
    fi
}

check_tools() {
    need docker "Install Docker Desktop and start it."
    if [ "$DRY_RUN" = 0 ] && ! docker compose version >/dev/null 2>&1; then
        die "docker compose is not available. Update Docker Desktop."
    fi
    if [ "$DRY_RUN" = 0 ] && ! docker info >/dev/null 2>&1; then
        die "Docker is installed but not running. Start Docker Desktop and try again."
    fi
    need curl "Install curl (Git for Windows includes it)."
    if [ "$WITH_UI" = 1 ]; then
        need npm "Install Node.js 20 or newer from nodejs.org."
        [ -f "$UI_DIR/package.json" ] || die "UI checkout not found at $UI_DIR. Clone Cansee_analytics_UI_APP next to this repo, or set CANSEE_UI_DIR."
    fi
}

ensure_env() {
    # docker-compose.dev.yml reads ../.env, so it must exist. Never overwrite one.
    if [ -f .env ]; then
        say "Using existing .env"
        # Without PUBLIC_SCHEME=http the backend assumes HTTPS: Secure-only
        # cookies and a year-long HSTS header on http://localhost.
        if ! grep -qE '^[[:space:]]*PUBLIC_SCHEME[[:space:]]*=[[:space:]]*http[[:space:]]*$' .env; then
            warn ".env does not set PUBLIC_SCHEME=http. Add that line for a local run, or sign-in may fail over http://localhost."
        fi
        return
    fi
    say "Creating a minimal .env (add API keys to it later)"
    local content
    content='# Local development settings, created by scripts/run-local.sh.
# Uncomment and fill in provider keys to run real scans and the agent
# workspace (/agent), then run scripts/run-local.sh again. Every other
# setting is listed in .env.prod.example.
# FIELD_ENCRYPTION_KEY is optional locally (secrets are then stored
# unencrypted, with a warning); generate one with: make generate-key
#
# The agent is on in dev and needs only keys. Required for chats, the
# writers and the research agents (the key needs the models
# claude-opus-5-5 and claude-haiku-5-5):
# ANTHROPIC_API_KEY=
# Google results for scans and research (one of the two):
# SERPAPI_KEY=
# SCRAPERAPI_KEY=
# The AI engines scans and research ask. An engine without a key is
# skipped and shown as not set up, never faked:
# OPENAI_API_KEY=
# GEMINI_API_KEY=
# PERPLEXITY_API_KEY=
# XAI_API_KEY=
# An account without a subscription has a small monthly AI allowance,
# which an agent reply or two can use up. Raise it for local testing:
# AI_FREE_MONTHLY_CAP_USD=20
# Turn the agent off:
# AGENT_ENABLED=false
DEBUG=true
PUBLIC_SCHEME=http
ALLOWED_HOSTS=localhost,127.0.0.1
FRONTEND_URL=http://localhost:5173'
    if [ "$DRY_RUN" = 1 ]; then
        echo "+ write .env:"
        # shellcheck disable=SC2001
        echo "$content" | sed 's/^/    /'
    else
        echo "$content" > .env
    fi
}

start_backend() {
    local services=(db redis web)
    if [ "$WITH_WORKERS" = 1 ]; then
        services+=(celery celery-beat)
        warn "Celery beat runs the scheduled jobs; with API keys in .env they spend credits."
    fi
    say "Building and starting: ${services[*]}"
    # --build: the code folder is mounted into the image, so an image built
    # from older requirements would start new code on old packages.
    run "${COMPOSE[@]}" up -d --build "${services[@]}"
}

wait_for_backend() {
    say "Waiting for the backend at $BACKEND_URL (migrations run first)"
    if [ "$DRY_RUN" = 1 ]; then
        echo "+ poll $BACKEND_URL/api/v1/version/ for up to ${WAIT_SECONDS}s"
        return
    fi
    local waited=0
    until curl -fsS -o /dev/null "$BACKEND_URL/api/v1/version/" 2>/dev/null; do
        if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx web; then
            echo
            warn "The backend container stopped. Last log lines:"
            "${COMPOSE[@]}" logs --tail 60 web || true
            die "Backend failed to start. Fix the error above, then run this script again."
        fi
        if [ "$waited" -ge "$WAIT_SECONDS" ]; then
            echo
            "${COMPOSE[@]}" logs --tail 60 web || true
            die "Backend did not answer within ${WAIT_SECONDS}s."
        fi
        printf '.'
        sleep 3
        waited=$((waited + 3))
    done
    echo
    say "Backend is up"
}

create_user() {
    local email="$1" name="$2"
    say "Creating login $email (you will be asked for a password)"
    # Git Bash needs winpty for an interactive prompt inside a container.
    local prefix=()
    case "$(uname -s)" in
        MINGW*|MSYS*) command -v winpty >/dev/null 2>&1 && prefix=(winpty) ;;
    esac
    run ${prefix[@]+"${prefix[@]}"} "${COMPOSE[@]}" exec web python manage.py create_user "$email" "$name"
}

start_ui() {
    cd "$UI_DIR"
    # Reinstall when node_modules is missing or older than the lockfile.
    if [ ! -d node_modules ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
        say "Installing UI dependencies (npm ci)"
        run npm ci
    fi
    echo
    say "Open $UI_URL/login"
    echo "    Backend API: $BACKEND_URL/api/v1/   Stop the UI with Ctrl+C."
    echo "    The backend keeps running afterwards; stop it with: scripts/run-local.sh stop"
    echo
    run npm run dev
}

# ── Arguments ────────────────────────────────────────────────────────
cmd="up"
case "${1:-}" in
    up|stop|status|logs|user|help) cmd="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
esac

positional=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --workers) WITH_WORKERS=1 ;;
        --no-ui)   WITH_UI=0 ;;
        --user)
            [ $# -ge 3 ] || die "--user needs an email and a name, e.g. --user you@example.com \"Your Name\""
            USER_EMAIL="$2"; USER_NAME="$3"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "Unknown option: $1 (see --help)" ;;
        *)  positional+=("$1") ;;
    esac
    shift
done

case "$cmd" in
    help) usage ;;
    up)
        [ ${#positional[@]} -eq 0 ] || die "Unexpected argument: ${positional[0]} (see --help)"
        check_tools
        ensure_env
        start_backend
        wait_for_backend
        if [ -n "$USER_EMAIL" ]; then
            create_user "$USER_EMAIL" "$USER_NAME"
        fi
        if [ "$WITH_UI" = 1 ]; then
            start_ui
        else
            say "Backend running at $BACKEND_URL. Stop it with: scripts/run-local.sh stop"
        fi
        ;;
    user)
        [ ${#positional[@]} -eq 2 ] || die "Usage: scripts/run-local.sh user EMAIL \"NAME\""
        create_user "${positional[0]}" "${positional[1]}"
        ;;
    stop)
        say "Stopping the containers (the database volume is kept)"
        run "${COMPOSE[@]}" down
        ;;
    status) run "${COMPOSE[@]}" ps ;;
    logs)   run "${COMPOSE[@]}" logs -f --tail 100 web ;;
esac
