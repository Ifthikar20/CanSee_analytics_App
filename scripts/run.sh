#!/usr/bin/env bash
#
# One-stop runner for every Python / Django / prod task in this repo.
#
# Usage:  scripts/run.sh <command> [args...]
#
# Run with no argument (or `help`) to print the menu below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# ── Prod SSH config (mirrors deploy.sh defaults) ─────────────────────
EC2_HOST="${EC2_HOST:-100.31.135.211}"
EC2_USER="${EC2_USER:-ubuntu}"
PEM_KEY="${PEM_KEY:-$HOME/.ssh/cansee-deploy.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/cansee/ftb-api-26}"
COMPOSE_FILE="docker/docker-compose.prod.yml"

# ── Style ────────────────────────────────────────────────────────────
B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; C=$'\033[0;36m'; N=$'\033[0m'

usage() {
  cat <<EOF
${C}${B}Cansee — task runner${N}

Usage: ${B}scripts/run.sh${N} ${G}<command>${N} [args...]

${B}Local commands${N} (run against your local Docker stack)
  ${G}create-user${N} <email> "<name>"   Create a user locally; prompts for the password
                                 (add --org SLUG to join an organization)
  ${G}migrate${N}                        Apply pending migrations
  ${G}makemigrations${N} [app]           Generate new migrations (review before commit)
  ${G}showmigrations${N} [app]           Show applied / pending migrations
  ${G}shell${N}                          Open a Django shell
  ${G}collectstatic${N}                  Collect static files
  ${G}test${N} [path]                    Run pytest
  ${G}check${N}                          Django system checks

${B}Prod commands${N} (SSH into the EC2 host)
  ${G}prod-migrate${N}                   Apply pending migrations on prod
  ${G}prod-showmigrations${N} [app]      Show migration status on prod
  ${G}prod-shell${N}                     Open a Django shell on prod
  ${G}prod-psql${N}                      Open a psql shell on the prod DB
  ${G}prod-create-user${N} <email> "<name>"  Create a user on prod; prompts for the password
                                 (add --org SLUG to join an organization)
  ${G}prod-logs${N} [request-id|-s svc]  Tail / grep prod logs (alias for scripts/logs.sh)
  ${G}prod-status${N}                    docker compose ps on prod

${B}Examples${N}
  scripts/run.sh create-user jane@example.com "Jane Doe"
  scripts/run.sh migrate
  scripts/run.sh prod-logs 530fb63c-56be-421c-ab7f-ca7ec0c72450
  scripts/run.sh prod-psql

${B}Env overrides${N} (same as deploy.sh)
  EC2_HOST=...  EC2_USER=...  PEM_KEY=/path/to/key.pem  REMOTE_DIR=...
EOF
}

# ── Local helpers ────────────────────────────────────────────────────
manage_local() {
  # If a docker compose stack is up locally, use it; otherwise fall back to host python.
  if docker compose ps web 2>/dev/null | grep -q "Up"; then
    docker compose exec -T web python manage.py "$@"
  else
    python manage.py "$@"
  fi
}

# ── Prod helpers ─────────────────────────────────────────────────────
ssh_remote() {
  if [[ ! -f "$PEM_KEY" ]]; then
    echo "${Y}PEM key not found at $PEM_KEY${N}" >&2
    echo "Set PEM_KEY=/path/to/key.pem or place cansee-deploy.pem in the repo root." >&2
    exit 1
  fi
  ssh -i "$PEM_KEY" -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR \
      "$EC2_USER@$EC2_HOST" "$@"
}

manage_remote() {
  ssh_remote "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE exec -T web python manage.py $*"
}

# ── Dispatch ─────────────────────────────────────────────────────────
cmd="${1:-help}"; shift || true

case "$cmd" in
  help|-h|--help)
    usage
    ;;

  # Local
  create-user)
    [[ $# -ge 2 ]] || { echo "Usage: scripts/run.sh create-user <email> \"<full name>\" [--org SLUG]" >&2; exit 1; }
    # create_user prompts for the password, so keep a TTY (no -T).
    if docker compose ps web 2>/dev/null | grep -q "Up"; then
      docker compose exec web python manage.py create_user "$@"
    else
      python manage.py create_user "$@"
    fi
    ;;
  migrate)            manage_local migrate --noinput ;;
  makemigrations)     manage_local makemigrations "$@" ;;
  showmigrations)     manage_local showmigrations "$@" ;;
  shell)              manage_local shell ;;
  collectstatic)      manage_local collectstatic --noinput ;;
  test)               manage_local test "$@" 2>/dev/null || pytest "$@" ;;
  check)              manage_local check ;;

  # Prod
  prod-migrate)       manage_remote migrate --noinput ;;
  prod-showmigrations) manage_remote showmigrations "$@" ;;
  prod-shell)
    ssh -i "$PEM_KEY" -t "$EC2_USER@$EC2_HOST" \
      "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE exec web python manage.py shell"
    ;;
  prod-psql)
    ssh -i "$PEM_KEY" -t "$EC2_USER@$EC2_HOST" \
      "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE exec db bash -c 'psql -U \$POSTGRES_USER -d \$POSTGRES_DB'"
    ;;
  prod-create-user)
    [[ $# -ge 2 ]] || { echo "Usage: scripts/run.sh prod-create-user <email> \"<full name>\" [--org SLUG]" >&2; exit 1; }
    echo "${Y}About to create a user on PROD: $1${N}"
    read -r -p "Continue? (y/N) " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 1; }
    # -t keeps a terminal end to end, so the password prompt runs inside
    # the container and the password is never a command-line argument.
    printf -v quoted '%q ' "$@"
    ssh -i "$PEM_KEY" -t "$EC2_USER@$EC2_HOST" \
      "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE exec web python manage.py create_user $quoted"
    ;;
  prod-logs)          exec "$SCRIPT_DIR/logs.sh" "$@" ;;
  prod-status)        ssh_remote "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE ps" ;;

  *)
    echo "${Y}Unknown command: $cmd${N}" >&2
    echo
    usage
    exit 1
    ;;
esac
