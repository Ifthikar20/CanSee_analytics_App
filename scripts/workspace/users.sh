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
#   ./users.sh passwd [email]           Reset a password (also clears a lockout)
#   ./users.sh check [email]            Diagnose a login that is not working
#   ./users.sh unlock [email]           Clear a lockout after failed logins
#   ./users.sh help                     Show this help
#
# Passwords are always typed at a hidden prompt, never passed on the
# command line. The app requires at least 12 characters and rejects
# common or all-numeric passwords.
#
# Logging in: emails are saved in lowercase and the login form is
# case-sensitive, so type the email in lowercase. Five wrong passwords
# lock that email for 30 minutes (the app still says "Incorrect email or
# password"); ./users.sh unlock or ./users.sh passwd clears it.

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


def clear_lockout(addr):
    """Remove django-axes failed-login records for this email."""
    try:
        from axes.models import AccessAttempt
    except Exception:
        return 0
    deleted, _ = AccessAttempt.objects.filter(username__iexact=addr).delete()
    return deleted


if mode == "unlock":
    print(f"cleared {clear_lockout(email)}")
    sys.exit(0)

if mode == "check":
    from django.conf import settings
    from django.utils import timezone

    if not existing:
        print("  user:            NOT FOUND in the local database")
        sys.exit(5)
    password_ok = existing.check_password(os.environ["CANSEE_PASSWORD"])
    print(f"  user:            {existing.email}")
    active_text = "yes" if existing.is_active else "NO"
    print(f"  active:          {active_text}")
    verified = "yes" if existing.is_email_verified else "NO"
    print(f"  email verified:  {verified}")
    pw_text = "matches" if password_ok else "DOES NOT MATCH"
    print(f"  password:        {pw_text}")

    locked, failures = False, 0
    try:
        from axes.models import AccessAttempt
        limit = getattr(settings, "AXES_FAILURE_LIMIT", 5)
        cooloff = getattr(settings, "AXES_COOLOFF_TIME", None)
        rows = list(AccessAttempt.objects.filter(username__iexact=email))
        failures = max((r.failures_since_start for r in rows), default=0)
        recent = [r for r in rows if cooloff is None or r.attempt_time >= timezone.now() - cooloff]
        locked = failures >= limit and bool(recent)
    except Exception:
        pass
    lock_text = f"LOCKED after {failures} failed logins" if locked else f"not locked ({failures} failed logins counted)"
    print(f"  lockout:         {lock_text}")

    typed_variants = set()
    try:
        from apps.accounts.models import LoginAttempt
        attempts = list(LoginAttempt.objects.filter(email__iexact=email).order_by("-timestamp")[:5])
    except Exception:
        attempts = None
    if attempts is None:
        print("  login attempts:  (not available on this branch)")
    elif not attempts:
        print("  login attempts:  none recorded for this email")
    else:
        print("  last login attempts (newest first):")
        for a in attempts:
            result = "success" if a.success else "failed"
            typed_variants.add(a.email)
            print(f"    {a.timestamp:%Y-%m-%d %H:%M:%S}  {result:<8} typed as {a.email}")

    print()
    problems = 0
    if not password_ok:
        problems += 1
        print(f"  -> The password you just typed is not the saved one. Reset it with: ./users.sh passwd {existing.email}")
    if locked:
        problems += 1
        print(f"  -> The account is locked by too many failed logins; even the right password is refused")
        print(f"     (shown as \"Incorrect email or password\"). Clear it with: ./users.sh unlock {existing.email}")
    if typed_variants - {existing.email}:
        problems += 1
        print(f"  -> The browser sent a different spelling/case of the email. Login is case-sensitive:")
        print(f"     type it exactly as {existing.email}")
    if attempts == [] :
        problems += 1
        print("  -> No login attempt from the browser reached this database. Make sure the browser is on")
        print("     the app started by ./run.sh (http://localhost:5173) and that ./run.sh is still running.")
    if not existing.is_active or not existing.is_email_verified:
        problems += 1
        print(f"  -> The account is inactive or unverified. Fix it with: ./users.sh passwd {existing.email}")
    if not problems:
        print("  -> Everything checks out in the database. Try logging in again with exactly this email and password.")
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
    existing.is_email_verified = True
    existing.save(update_fields=["password", "is_active", "is_email_verified"])
    clear_lockout(email)
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
    IFS= read -r -s -p "  Password (hidden): " password; echo >&2
    IFS= read -r -s -p "  Repeat password: " confirm; echo >&2
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
  ok "Created $(printf '%s' "$email" | tr '[:upper:]' '[:lower:]'). Log in at http://localhost:5173 with the email in lowercase."
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

cmd_check() {
  ensure_backend
  step "Check a login"
  local email password
  email="$(ask_email "${1:-}")"
  IFS= read -r -s -p "  Password to test (hidden): " password; echo
  echo
  local status=0
  CANSEE_MODE=check CANSEE_EMAIL="$email" CANSEE_PASSWORD="$password" run_py || status=$?
  [ "$status" = "5" ] && die "No user with email $email. Create one with: ./users.sh add $email"
  [ "$status" = "0" ] || die "The check failed (exit $status)."
}

cmd_unlock() {
  ensure_backend
  step "Unlock a login"
  local email result
  email="$(ask_email "${1:-}")"
  result="$(CANSEE_MODE=unlock CANSEE_EMAIL="$email" CANSEE_PASSWORD="" run_py | tail -n 1)"
  ok "Lockout cleared for $email (${result#cleared } record(s) removed). You can log in again now."
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
    check | test) cmd_check "$@" ;;
    unlock) cmd_unlock "$@" ;;
    -h | --help | help) usage ;;
    *) usage; die "Unknown command: $cmd" ;;
  esac
}

main "$@"
