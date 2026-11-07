#!/usr/bin/env bash
#
# Interactive create-user helper.
#
# Prompts for env (local / prod), email, full name and an optional
# organization slug, then runs `manage.py create_user` locally or, on
# prod, inside the web container over SSH. The password is typed at the
# command's own hidden prompt; it is never passed as an argument.
#
# Public registration is disabled on prod (SIGNUPS_ENABLED=False), so
# this script is the canonical way to provision an account.
#
# Usage:
#   scripts/create_user.sh                                   # full prompts
#   scripts/create_user.sh local                             # skip env prompt
#   scripts/create_user.sh prod jane@example.com "Jane Doe"
#   scripts/create_user.sh prod jane@example.com "Jane Doe" acme   # join org "acme"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# ── Style ────────────────────────────────────────────────────────────
B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'
C=$'\033[0;36m'; D=$'\033[2m'; N=$'\033[0m'

# ── Prod SSH config (mirrors deploy.sh defaults) ─────────────────────
EC2_HOST="${EC2_HOST:-100.31.135.211}"
EC2_USER="${EC2_USER:-ubuntu}"
PEM_KEY="${PEM_KEY:-$HOME/.ssh/cansee-deploy.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/cansee/ftb-api-26}"
COMPOSE_FILE="docker/docker-compose.prod.yml"

# ── Args (all optional) ──────────────────────────────────────────────
env_choice="${1:-}"
email_arg="${2:-}"
name_arg="${3:-}"
org_arg="${4:-}"

# ── Env prompt ───────────────────────────────────────────────────────
if [[ -z "$env_choice" ]]; then
  printf "${B}Where do you want to create the user?${N}\n"
  printf "  ${G}1${N} local  (runs against your local DB)\n"
  printf "  ${G}2${N} prod   (SSH into EC2 and create on the live DB)\n"
  printf "Pick [1/2, default 1]: "
  read -r pick
  case "$pick" in
    2|prod) env_choice="prod" ;;
    *)      env_choice="local" ;;
  esac
fi

# ── Account prompts ──────────────────────────────────────────────────
if [[ -z "$email_arg" ]]; then
  printf "${B}Email:${N} "
  read -r email_arg
fi
[[ -z "$email_arg" ]] && { printf "${R}Email is required.${N}\n" >&2; exit 1; }

if [[ -z "$name_arg" ]]; then
  printf "${B}Full name:${N} "
  read -r name_arg
fi
[[ -z "$name_arg" ]] && { printf "${R}Full name is required.${N}\n" >&2; exit 1; }

if [[ $# -lt 4 ]]; then
  printf "${B}Organization slug${N} ${D}(blank = none)${N}: "
  read -r org_arg
fi

cmd_args=(create_user "$email_arg" "$name_arg")
[[ -n "$org_arg" ]] && cmd_args+=(--org "$org_arg")

# ── Confirm prod ─────────────────────────────────────────────────────
if [[ "$env_choice" == "prod" ]]; then
  printf "\n${Y}About to create ${B}%s${N}${Y} on PROD.${N}\n" "$email_arg"
  printf "Continue? (y/N) "
  read -r ans
  case "$ans" in
    y|Y|yes) ;;
    *) printf "Aborted.\n"; exit 1 ;;
  esac
fi

printf "\n${C}→ creating user (you will be asked for the password)...${N}\n"

# ── Local path ───────────────────────────────────────────────────────
if [[ "$env_choice" == "local" ]]; then
  # Prefer the project virtualenv; fall back to system python.
  if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    py="$REPO_DIR/.venv/bin/python"
  elif [[ -x "$REPO_DIR/venv/bin/python" ]]; then
    py="$REPO_DIR/venv/bin/python"
  else
    py="python3"
  fi
  exec "$py" manage.py "${cmd_args[@]}"
fi

# ── Prod path ────────────────────────────────────────────────────────
if [[ ! -f "$PEM_KEY" ]]; then
  printf "${R}PEM key not found at %s${N}\n" "$PEM_KEY" >&2
  printf "Set PEM_KEY=/path/to/cansee-deploy.pem.\n" >&2
  exit 1
fi

# -t gives the remote command a terminal, so the password prompt runs
# inside the container and the password never leaves that session.
printf -v quoted '%q ' "${cmd_args[@]}"
exec ssh -t -i "$PEM_KEY" -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR \
  "$EC2_USER@$EC2_HOST" \
  "cd $REMOTE_DIR && docker compose -f $COMPOSE_FILE exec web python manage.py $quoted"
