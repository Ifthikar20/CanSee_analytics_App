#!/usr/bin/env bash
#
# Cansee local runner: sets up and starts the whole app (backend + UI)
# from one command.
#
# Put this file in the folder that holds (or should hold) both repos:
#
#   CANSEE/
#     run.sh                      <- this script
#     CanSee_analytics_App/       <- Django backend (cloned if missing)
#     Cansee_analytics_UI_APP/    <- Vue frontend  (cloned if missing)
#
# It also works from inside the backend repo (scripts/workspace/run.sh)
# or from a sibling folder such as CANSEE/CanSee_console_app/; in both
# cases it uses the CANSEE folder. Set CANSEE_WORKSPACE to override.
#
# Windows: run it from Git Bash (installed with Git for Windows).
# macOS / Linux: run it from any terminal.
#
# Usage:
#   ./run.sh              Set up everything and start the app (default)
#   ./run.sh setup        Clone/update repos, install, migrate. No servers.
#   ./run.sh user         Create a login (email, name, password)
#   ./run.sh stop         Stop the servers and the Docker database/Redis
#   ./run.sh status       Show what is running
#   ./run.sh logs         Follow the server logs
#   ./run.sh help         Show this help
#
# Options (for the default start command):
#   --no-pull       Do not git pull existing repos
#   --no-browser    Do not open the browser
#
# Requirements: Git, Docker Desktop (running), Python 3.12, Node.js 20+.
# The backend .env must contain ANTHROPIC_API_KEY. If .env is missing the
# script creates it and asks for the key (input is hidden).

set -euo pipefail

# ── Locations ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${CANSEE_WORKSPACE:-}" ]; then
  # Explicit override.
  WORKSPACE="$(cd "$CANSEE_WORKSPACE" && pwd)"
elif [ -f "$SCRIPT_DIR/../../manage.py" ]; then
  # Running from CanSee_analytics_App/scripts/workspace/
  WORKSPACE="$(cd "$SCRIPT_DIR/../../.." && pwd)"
elif [ ! -d "$SCRIPT_DIR/CanSee_analytics_App" ] && [ -d "$SCRIPT_DIR/../CanSee_analytics_App" ]; then
  # Running from a sibling folder such as CANSEE/CanSee_console_app/
  WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  WORKSPACE="$SCRIPT_DIR"
fi

BACKEND_DIR="$WORKSPACE/CanSee_analytics_App"
UI_DIR="$WORKSPACE/Cansee_analytics_UI_APP"
BACKEND_REPO="${BACKEND_REPO:-https://github.com/Ifthikar20/CanSee_analytics_App.git}"
UI_REPO="${UI_REPO:-https://github.com/Ifthikar20/Cansee_analytics_UI_APP.git}"
BACKEND_BRANCH="${BACKEND_BRANCH:-main}"
UI_BRANCH="${UI_BRANCH:-main}"

RUN_DIR="$WORKSPACE/.cansee-run"
LOG_DIR="$RUN_DIR/logs"
COMPOSE_FILE="docker/docker-compose.dev.yml"

BACKEND_HOST="127.0.0.1"
BACKEND_PORT="${BACKEND_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"
BACKEND_URL="http://$BACKEND_HOST:$BACKEND_PORT"
UI_URL="http://localhost:$UI_PORT"

export DJANGO_SETTINGS_MODULE="config.settings.dev"
export PYTHONUNBUFFERED=1

SKIP_PULL="${SKIP_PULL:-0}"
NO_BROWSER="${NO_BROWSER:-0}"

IS_WINDOWS=0
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) IS_WINDOWS=1 ;;
esac

# ── Output helpers ───────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; C=$'\033[0;36m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; C=""; N=""
fi

step() { printf '\n%s==> %s%s\n' "$C$B" "$*" "$N"; }
ok()   { printf '%s  ok%s  %s\n' "$G" "$N" "$*"; }
warn() { printf '%s  warning%s  %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s  error%s  %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# ── Prerequisites ────────────────────────────────────────────────────
PYTHON_CMD=()

find_python() {
  local candidates=("py -3.12" "python3.12" "python3" "python")
  local c
  for c in "${candidates[@]}"; do
    # shellcheck disable=SC2086  # intentional word splitting for "py -3.12"
    if command -v ${c%% *} >/dev/null 2>&1 &&
       ${c} -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
      read -r -a PYTHON_CMD <<<"$c"
      return 0
    fi
  done
  return 1
}

check_prereqs() {
  step "Checking prerequisites"

  command -v git >/dev/null 2>&1 || die "Git is not installed. Get it from https://git-scm.com"
  ok "git"

  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Desktop."
  docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop and try again."
  docker compose version >/dev/null 2>&1 || die "'docker compose' is not available. Update Docker Desktop."
  ok "docker"

  find_python || die "Python 3.12 not found. Install Python 3.12 from https://www.python.org/downloads/ (the pinned dependencies do not support 3.13 yet)."
  ok "python ($("${PYTHON_CMD[@]}" --version 2>&1))"

  command -v node >/dev/null 2>&1 || die "Node.js is not installed. Install Node.js 20 or newer from https://nodejs.org"
  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "$node_major" -ge 20 ] || die "Node.js $node_major found; version 20 or newer is required."
  command -v npm >/dev/null 2>&1 || die "npm is not available."
  ok "node ($(node --version))"

  command -v curl >/dev/null 2>&1 || die "curl is not available."
}

# ── Repositories ─────────────────────────────────────────────────────
sync_repo() {
  local dir="$1" url="$2" branch="$3" name
  name="$(basename "$dir")"

  if [ ! -d "$dir/.git" ]; then
    if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
      die "$dir exists but is not a git repository. Move it aside and run again."
    fi
    echo "  cloning $name ($branch)"
    git clone --branch "$branch" "$url" "$dir"
    ok "$name cloned"
    return
  fi

  if [ "$SKIP_PULL" = "1" ]; then
    ok "$name (pull skipped)"
    return
  fi
  if [ -n "$(git -C "$dir" status --porcelain)" ]; then
    warn "$name has uncommitted changes; not pulling."
    return
  fi
  local current
  current="$(git -C "$dir" rev-parse --abbrev-ref HEAD)"
  if git -C "$dir" pull --ff-only --quiet; then
    ok "$name up to date (branch $current)"
  else
    warn "$name: 'git pull' failed on branch $current; continuing with the local copy."
  fi
}

sync_repos() {
  step "Getting the code"
  sync_repo "$BACKEND_DIR" "$BACKEND_REPO" "$BACKEND_BRANCH"
  sync_repo "$UI_DIR" "$UI_REPO" "$UI_BRANCH"
}

# ── Backend .env ─────────────────────────────────────────────────────
env_value() {
  # Print the value of KEY from the backend .env (empty if missing).
  local key="$1"
  [ -f "$BACKEND_DIR/.env" ] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$key=//p" "$BACKEND_DIR/.env" |
    tail -n 1 | tr -d '\r' | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//'
}

ensure_env() {
  step "Checking backend settings (.env)"
  local env_file="$BACKEND_DIR/.env"

  if [ ! -f "$env_file" ]; then
    echo "  No .env found. Creating $env_file"
    local key=""
    while [ -z "$key" ]; do
      read -r -s -p "  Paste your Anthropic API key (hidden): " key
      echo
    done
    (
      umask 077
      cat >"$env_file" <<EOF
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_SECRET_KEY=local-dev-only-$(date +%s)
ANTHROPIC_API_KEY=$key
EOF
    )
    ok ".env created"
    return
  fi

  if [ -z "$(env_value ANTHROPIC_API_KEY)" ]; then
    die "ANTHROPIC_API_KEY is missing or empty in $env_file. Add a line: ANTHROPIC_API_KEY=your-key"
  fi
  ok ".env has ANTHROPIC_API_KEY"

  local other=0 k
  for k in OPENAI_API_KEY GEMINI_API_KEY PERPLEXITY_API_KEY; do
    [ -n "$(env_value "$k")" ] && other=1
  done
  [ "$other" = "1" ] || echo "  note: only Claude is configured; select Claude when you run an audit."
}

# ── Docker services ──────────────────────────────────────────────────
compose() { (cd "$BACKEND_DIR" && docker compose -f "$COMPOSE_FILE" "$@"); }

start_services() {
  step "Starting Postgres and Redis (Docker)"
  compose up -d db redis >/dev/null || die "Could not start Docker services. Is port 5432 or 6379 already used by a local Postgres/Redis?"

  local i
  for i in $(seq 1 60); do
    if compose exec -T db pg_isready -U postgres >/dev/null 2>&1; then
      ok "Postgres ready"
      break
    fi
    [ "$i" = "60" ] && die "Postgres did not become ready in 60 seconds. Check: docker compose -f $COMPOSE_FILE logs db"
    sleep 1
  done

  for i in $(seq 1 30); do
    if compose exec -T redis redis-cli ping >/dev/null 2>&1; then
      ok "Redis ready"
      return
    fi
    sleep 1
  done
  die "Redis did not become ready in 30 seconds."
}

# ── Backend install ──────────────────────────────────────────────────
venv_python() {
  if [ -f "$BACKEND_DIR/.venv/Scripts/python.exe" ]; then
    echo "$BACKEND_DIR/.venv/Scripts/python.exe"
  else
    echo "$BACKEND_DIR/.venv/bin/python"
  fi
}

manage() { (cd "$BACKEND_DIR" && "$(venv_python)" manage.py "$@"); }

setup_backend() {
  step "Setting up the backend"
  if [ ! -x "$(venv_python)" ] && [ ! -f "$(venv_python)" ]; then
    echo "  creating virtual environment (.venv)"
    (cd "$BACKEND_DIR" && "${PYTHON_CMD[@]}" -m venv .venv)
  fi
  local py stamp want
  py="$(venv_python)"
  stamp="$BACKEND_DIR/.venv/.cansee-requirements"
  want="$(cat "$BACKEND_DIR/requirements/base.txt" "$BACKEND_DIR/requirements/dev.txt" | cksum)"
  if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
    echo "  installing Python packages (first run takes a few minutes)"
    "$py" -m pip install --quiet --upgrade pip
    (cd "$BACKEND_DIR" && "$py" -m pip install --quiet -r requirements/dev.txt)
    echo "$want" >"$stamp"
  fi
  ok "Python packages installed"

  echo "  applying database migrations"
  manage migrate --noinput >"$LOG_DIR/migrate.log" 2>&1 ||
    { tail -n 30 "$LOG_DIR/migrate.log"; die "Migrations failed. Full log: $LOG_DIR/migrate.log"; }
  ok "database up to date"

  manage seed_prompts >"$LOG_DIR/seed.log" 2>&1 ||
    warn "Prompt library seeding failed (see $LOG_DIR/seed.log). The app still runs."
  ok "prompt library seeded"

  if manage check_llm_keys --provider claude >"$LOG_DIR/llm-check.log" 2>&1; then
    ok "Anthropic key works"
  else
    warn "Anthropic key check failed. Audits will not work until it is fixed. Details: $LOG_DIR/llm-check.log"
  fi
}

# ── Frontend install ─────────────────────────────────────────────────
setup_frontend() {
  step "Setting up the frontend"
  local stamp want
  stamp="$UI_DIR/node_modules/.cansee-lock"
  want="$(cat "$UI_DIR/package.json" "$UI_DIR/package-lock.json" 2>/dev/null | cksum)"
  if [ ! -d "$UI_DIR/node_modules" ] || [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
    echo "  installing npm packages (first run takes a minute or two)"
    (cd "$UI_DIR" && npm install --no-audit --no-fund --loglevel=error)
    echo "$want" >"$stamp"
  fi
  ok "npm packages installed"
}

# ── Users ────────────────────────────────────────────────────────────
# The Python below reads everything from environment variables so the
# password never appears on a command line or in the process list.
USER_PY='
import os, sys
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

User = get_user_model()
mode = os.environ["CANSEE_MODE"]

if mode == "count":
    print(User.objects.count())
    sys.exit(0)

email = os.environ["CANSEE_EMAIL"].strip().lower()
name = os.environ.get("CANSEE_NAME", "").strip() or email.split("@")[0]
password = os.environ["CANSEE_PASSWORD"]
existing = User.objects.filter(email__iexact=email).first()

if mode == "exists":
    print("yes" if existing else "no")
    sys.exit(0)

try:
    validate_password(password, user=existing)
except ValidationError as exc:
    for msg in exc.messages:
        print("  - " + msg)
    sys.exit(3)

if existing:
    existing.set_password(password)
    existing.is_active = True
    existing.save(update_fields=["password", "is_active"])
    print("updated")
else:
    User.objects.create_user(email=email, password=password, full_name=name, is_email_verified=True)
    print("created")
'

run_user_py() { manage shell -c "$USER_PY"; }

user_count() { CANSEE_MODE=count run_user_py 2>/dev/null | tail -n 1; }

create_user() {
  step "Create a login"
  local email name password confirm exists result
  while true; do
    read -r -p "  Email: " email
    if [[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then break; fi
    echo "  That does not look like an email address."
  done

  exists="$(CANSEE_MODE=exists CANSEE_EMAIL="$email" CANSEE_PASSWORD="" run_user_py | tail -n 1)"
  if [ "$exists" = "yes" ]; then
    local answer
    read -r -p "  $email already exists. Reset its password? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "  Left unchanged."; return 0; }
    name=""
  else
    read -r -p "  Full name: " name
  fi

  while true; do
    read -r -s -p "  Password (hidden): " password; echo
    read -r -s -p "  Repeat password: " confirm; echo
    if [ "$password" != "$confirm" ]; then echo "  Passwords do not match."; continue; fi
    if [ -z "$password" ]; then echo "  Password cannot be empty."; continue; fi
    local status=0
    result="$(CANSEE_MODE=save CANSEE_EMAIL="$email" CANSEE_NAME="$name" CANSEE_PASSWORD="$password" run_user_py 2>&1)" ||
      status=$?
    if [ "$status" = "3" ]; then
      echo "  Password rejected:"
      echo "$result" | grep '^  - '
      continue
    fi
    [ "$status" = "0" ] || { echo "$result" | tail -n 20; die "Could not save the user."; }
    break
  done

  case "$(echo "$result" | tail -n 1)" in
    created) ok "Login created for $email" ;;
    updated) ok "Password reset for $email" ;;
    *) ok "Saved $email" ;;
  esac
}

maybe_create_first_user() {
  local count
  count="$(user_count || echo "")"
  if [ "$count" = "0" ]; then
    echo
    echo "  No logins exist yet. Let's create one."
    create_user
  fi
}

# ── Process management ───────────────────────────────────────────────
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

kill_tree() {
  # kill_tree <msys/unix pid> [windows pid]
  local pid="$1" winpid="${2:-}"
  [ -n "$pid" ] || return 0
  if [ "$IS_WINDOWS" = "1" ]; then
    if [ -z "$winpid" ] && [ -r "/proc/$pid/winpid" ]; then winpid="$(cat "/proc/$pid/winpid")"; fi
    if [ -n "$winpid" ]; then
      taskkill //F //T //PID "$winpid" >/dev/null 2>&1 || true
    fi
    kill "$pid" 2>/dev/null || true
  else
    # Each server runs in its own process group (set -m), so kill the group.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
}

save_pid() {
  local name="$1" pid="$2" winpid=""
  if [ "$IS_WINDOWS" = "1" ] && [ -r "/proc/$pid/winpid" ]; then winpid="$(cat "/proc/$pid/winpid")"; fi
  echo "$pid $winpid" >"$RUN_DIR/$name.pid"
}

stop_one() {
  local name="$1" file="$RUN_DIR/$1.pid" pid winpid
  [ -f "$file" ] || return 0
  read -r pid winpid <"$file" || true
  kill_tree "$pid" "$winpid"
  rm -f "$file"
  ok "$name stopped"
}

wait_for_http() {
  # wait_for_http <url> <seconds> <pid> <logfile> <label>
  local url="$1" timeout="$2" pid="$3" log="$4" label="$5" i code
  for i in $(seq 1 "$timeout"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      ok "$label is up ($url)"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo; tail -n 40 "$log"
      die "$label stopped while starting. Full log: $log"
    fi
    sleep 1
  done
  tail -n 40 "$log"
  die "$label did not respond within $timeout seconds. Full log: $log"
}

open_browser() {
  [ "$NO_BROWSER" = "1" ] && return 0
  if [ "$IS_WINDOWS" = "1" ]; then
    cmd.exe //c start "" "$1" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 || true
  fi
}

BACKEND_PID=""
FRONTEND_PID=""
TAIL_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  step "Shutting down servers"
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
  stop_one frontend
  stop_one backend
  echo "  Postgres and Redis are still running in Docker (fast restart)."
  echo "  Run './run.sh stop' to stop them too."
}

start_servers() {
  step "Starting the app"
  port_in_use "$BACKEND_PORT" && die "Port $BACKEND_PORT is already in use. Run './run.sh stop' or close the program using it."
  port_in_use "$UI_PORT" && die "Port $UI_PORT is already in use. Run './run.sh stop' or close the program using it."

  set -m  # give each server its own process group so it can be stopped cleanly
  (cd "$BACKEND_DIR" && exec "$(venv_python)" manage.py runserver "$BACKEND_HOST:$BACKEND_PORT") \
    >"$LOG_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
  save_pid backend "$BACKEND_PID"

  # Point the Vite dev proxy at 127.0.0.1 explicitly: on Windows,
  # "localhost" can resolve to IPv6 (::1) while Django listens on IPv4.
  (cd "$UI_DIR" && VITE_API_URL="$BACKEND_URL" VITE_WS_URL="ws://$BACKEND_HOST:$BACKEND_PORT" \
    exec npm run dev -- --port "$UI_PORT" --strictPort) \
    >"$LOG_DIR/frontend.log" 2>&1 &
  FRONTEND_PID=$!
  save_pid frontend "$FRONTEND_PID"
  set +m

  trap cleanup INT TERM EXIT

  wait_for_http "$BACKEND_URL/health/" 90 "$BACKEND_PID" "$LOG_DIR/backend.log" "Backend"
  wait_for_http "$UI_URL/" 90 "$FRONTEND_PID" "$LOG_DIR/frontend.log" "Frontend"

  open_browser "$UI_URL"

  cat <<EOF

${G}${B}Cansee is running.${N}

  App:        $UI_URL
  API:        $BACKEND_URL
  Logs:       $LOG_DIR
  New login:  ./run.sh user   (in another terminal)

Live logs follow below. Press Ctrl+C to stop the servers.

EOF

  tail -n 0 -f "$LOG_DIR/backend.log" "$LOG_DIR/frontend.log" &
  TAIL_PID=$!

  # Exit (and clean up) as soon as either server stops.
  while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
    sleep 2
  done
  warn "A server stopped unexpectedly. See the logs above or in $LOG_DIR."
}

# ── Commands ─────────────────────────────────────────────────────────
prepare() {
  mkdir -p "$LOG_DIR"
  check_prereqs
  sync_repos
  ensure_env
  start_services
  setup_backend
  setup_frontend
}

cmd_start() {
  prepare
  maybe_create_first_user
  start_servers
}

cmd_setup() {
  prepare
  maybe_create_first_user
  step "Setup complete. Run ./run.sh to start the app."
}

cmd_user() {
  mkdir -p "$LOG_DIR"
  [ -d "$BACKEND_DIR" ] || die "Backend not found at $BACKEND_DIR. Run ./run.sh setup first."
  [ -f "$(venv_python)" ] || die "Backend is not installed yet. Run ./run.sh setup first."
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop."
  start_services
  create_user
}

cmd_stop() {
  step "Stopping Cansee"
  stop_one frontend
  stop_one backend
  if [ -d "$BACKEND_DIR" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    compose stop db redis >/dev/null 2>&1 || true
    ok "Postgres and Redis stopped (data is kept)"
  fi
}

cmd_status() {
  step "Status"
  local name file pid winpid
  for name in backend frontend; do
    file="$RUN_DIR/$name.pid"
    if [ -f "$file" ]; then
      read -r pid winpid <"$file" || true
      if kill -0 "$pid" 2>/dev/null; then ok "$name running (pid $pid)"; else warn "$name not running (stale pid file)"; fi
    else
      echo "  $name not running"
    fi
  done
  if [ -d "$BACKEND_DIR" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    compose ps db redis
  fi
}

cmd_logs() {
  [ -d "$LOG_DIR" ] || die "No logs yet. Start the app with ./run.sh"
  tail -n 50 -f "$LOG_DIR"/backend.log "$LOG_DIR"/frontend.log
}

main() {
  local cmd="start"
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-pull) SKIP_PULL=1 ;;
      --no-browser) NO_BROWSER=1 ;;
      -h | --help | help) usage; exit 0 ;;
      start | setup | user | stop | status | logs) cmd="$1" ;;
      *) usage; die "Unknown command or option: $1" ;;
    esac
    shift
  done

  case "$cmd" in
    start) cmd_start ;;
    setup) cmd_setup ;;
    user) cmd_user ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
  esac
}

main "$@"
