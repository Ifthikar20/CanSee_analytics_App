#!/usr/bin/env bash
#
# Cansee local user manager: create logins, list them, reset passwords.
# Existing users are never deleted.
#
# Put this file next to run.sh (for example in CANSEE/CanSee_console_app/
# or directly in CANSEE/). It finds CanSee_analytics_App the same way
# run.sh does. Run ./run.sh once first so the backend is installed.
#
# Windows: run it from Git Bash. macOS / Linux: any terminal.
#
# Usage:
#   ./users.sh                          Create a user (asks for everything)
#   ./users.sh add [email] [full name]  Create a user (asks for what is missing)
#   ./users.sh list                     List all users
#   ./users.sh passwd [email]           Reset a user's password
#   ./users.sh help                     Show this help
#
# Passwords are always typed at a hidden prompt, never passed on the
# command line. The app requires at least 12 characters and rejects
# common or all-numeric passwords.

set -euo pipefail

# ── Locations (same rules as run.sh) ────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${CANSEE_WORKSPACE:-}" ]; then
  WORKSPACE="$(cd "$CANSEE_WORKSPACE" && pwd)"
elif [ -f "$SCRIPT_DIR/../../manage.py" ]; then
  WORKSPACE="$(cd "$SCRIPT_DIR/../../.." && pwd)"
elif [ ! -d "$SCRIPT_DIR/CanSee_analytics_App" ] && [ -d "$SCRIPT_DIR/../CanSee_analytics_App" ]; then
  WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  WORKSPACE="$SCRIPT_DIR"
fi
BACKEND_DIR="$WORKSPACE/CanSee_analytics_App"
COMPOSE_FILE="docker/docker-compose.dev.yml"

# Same database settings run.sh uses (Docker Postgres on 127.0.0.1).
export DJANGO_SETTINGS_MODULE="config.settings.dev"
export PYTHONUNBUFFERED=1 PYTHONUTF8=1
export DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=cansee DB_USER=postgres DB_PASSWORD=postgres
export REDIS_URL=redis://127.0.0.1:6379/0 CELERY_BROKER_URL=redis://127.0.0.1:6379/1
export INTELLIGENCE_SERVICE_URL="" SOURCES_SERVICE_URL=""

# ── Output helpers ───────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[0;32m'; R=$'\033[0;31m'; C=$'\033[0;36m'; N=$'\033[0m'
else
  B=""; G=""; R=""; C=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$C$B" "$*" "$N"; }
ok()   { printf '%s  ok%s  %s\n' "$G" "$N" "$*"; }
die()  { printf '%s  error%s  %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# ── Backend access ───────────────────────────────────────────────────
venv_python() {
  if [ -f "$BACKEND_DIR/.venv/Scripts/python.exe" ]; then
    echo "$BACKEND_DIR/.venv/Scripts/python.exe"
  else
    echo "$BACKEND_DIR/.venv/bin/python"
  fi
}

compose() { (cd "$BACKEND_DIR" && docker compose -f "$COMPOSE_FILE" "$@"); }

ensure_backend() {
  [ -f "$BACKEND_DIR/manage.py" ] || die "Backend not found at $BACKEND_DIR. Run ./run.sh first."
  [ -f "$(venv_python)" ] || die "Backend is not installed yet. Run ./run.sh first."
  command -v docker >/dev/null 2>&1 || die "Docker is not installed."
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop."
  if ! compose exec -T db pg_isready -U postgres >/dev/null 2>&1; then
    echo "  starting the database (Docker)"
    compose up -d db redis >/dev/null 2>&1 || die "Could not start the Docker database."
    local _
    for _ in $(seq 1 60); do
      compose exec -T db pg_isready -U postgres >/dev/null 2>&1 && return 0
      sleep 1
    done
    die "The database did not become ready in 60 seconds."
  fi
}

# All database work happens in this Python snippet. Inputs arrive through
# environment variables so passwords never appear on a command line.
USER_PY='
import os, sys
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

User = get_user_model()
mode = os.environ["CANSEE_MODE"]

if mode == "list":
    users = User.objects.order_by("email")
    if not users:
        print("  (no users yet)")
    for u in users:
        joined = getattr(u, "created_at", None) or getattr(u, "date_joined", None)
        joined = joined.strftime("%Y-%m-%d") if joined else "-"
        last = u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "never"
        status = "active" if u.is_active else "inactive"
        print(f"  {u.email:<36} {u.full_name[:24]:<24} {status:<8} joined {joined}  last login {last}")
    print(f"  total: {users.count()}")
    sys.exit(0)

email = os.environ["CANSEE_EMAIL"].strip().lower()
existing = User.objects.filter(email__iexact=email).first()

if mode == "exists":
    print("yes" if existing else "no")
    sys.exit(0)

password = os.environ["CANSEE_PASSWORD"]
try:
    validate_password(password, user=existing)
except ValidationError as exc:
    for msg in exc.messages:
        print("  - " + msg)
    sys.exit(3)

if mode == "add":
    if existing:
        print("exists")
        sys.exit(4)
    name = os.environ.get("CANSEE_NAME", "").strip() or email.split("@")[0]
    User.objects.create_user(email=email, password=password, full_name=name, is_email_verified=True)
    print("created")
elif mode == "passwd":
    if not existing:
        print("missing")
        sys.exit(5)
    existing.set_password(password)
    existing.is_active = True
    existing.save(update_fields=["password", "is_active"])
    print("updated")
'

run_py() { (cd "$BACKEND_DIR" && "$(venv_python)" manage.py shell -c "$USER_PY"); }

# ── Prompts ──────────────────────────────────────────────────────────
ask_email() {
  # ask_email <preset>  -> prints a valid email
  local email="$1"
  while ! [[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; do
    [ -n "$email" ] && echo "  '$email' does not look like an email address." >&2
    read -r -p "  Email: " email
  done
  printf '%s' "$email"
}

user_exists() {
  CANSEE_MODE=exists CANSEE_EMAIL="$1" run_py 2>/dev/null | tail -n 1
}

# save_with_password <mode> <email> <name>: prompts until the app accepts
# the password, then saves. Prints the result word (created/updated).
save_with_password() {
  local mode="$1" email="$2" name="$3" password confirm result status
  while true; do
    read -r -s -p "  Password (hidden): " password; echo >&2
    read -r -s -p "  Repeat password: " confirm; echo >&2
    if [ -z "$password" ]; then echo "  Password cannot be empty." >&2; continue; fi
    if [ "$password" != "$confirm" ]; then echo "  Passwords do not match." >&2; continue; fi
    status=0
    result="$(CANSEE_MODE="$mode" CANSEE_EMAIL="$email" CANSEE_NAME="$name" CANSEE_PASSWORD="$password" run_py 2>&1)" ||
      status=$?
    if [ "$status" = "3" ]; then
      echo "  Password rejected:" >&2
      echo "$result" | grep '^  - ' >&2
      continue
    fi
    [ "$status" = "0" ] || { echo "$result" | tail -n 20 >&2; die "Could not save the user."; }
    echo "$result" | tail -n 1
    return 0
  done
}

# ── Commands ─────────────────────────────────────────────────────────
cmd_add() {
  ensure_backend
  step "Create a user"
  local email name
  email="$(ask_email "${1:-}")"
  if [ "$(user_exists "$email")" = "yes" ]; then
    die "$email already exists. To change its password run: ./users.sh passwd $email"
  fi
  name="${2:-}"
  [ -n "$name" ] || read -r -p "  Full name: " name
  save_with_password add "$email" "$name" >/dev/null
  ok "Created $email. Log in at http://localhost:5173"
}

cmd_passwd() {
  ensure_backend
  step "Reset a password"
  local email
  email="$(ask_email "${1:-}")"
  [ "$(user_exists "$email")" = "yes" ] || die "No user with email $email. Create one with: ./users.sh add $email"
  save_with_password passwd "$email" "" >/dev/null
  ok "Password updated for $email"
}

cmd_list() {
  ensure_backend
  step "Users"
  CANSEE_MODE=list run_py
}

main() {
  local cmd="${1:-add}"
  [ $# -gt 0 ] && shift
  case "$cmd" in
    add | new | create) cmd_add "$@" ;;
    list | ls) cmd_list ;;
    passwd | password | reset-password) cmd_passwd "$@" ;;
    -h | --help | help) usage ;;
    *) usage; die "Unknown command: $cmd" ;;
  esac
}

main "$@"
