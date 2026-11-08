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
#   ./run.sh user         Create a login (uses users.sh when present)
#   ./run.sh stop         Stop the servers and the Docker database/Redis
#   ./run.sh status       Show what is running
#   ./run.sh reset-db     Delete the local database and start it fresh
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
# Write logs as UTF-8 on Windows instead of the console code page.
export PYTHONUTF8=1

# Django runs natively here, not inside the compose network, so it must
# reach Postgres/Redis through the ports Docker publishes on 127.0.0.1.
# These match the db/redis services in docker/docker-compose.dev.yml.
# Exported variables take precedence over the backend .env (django-environ
# only fills in values that are not already set), so a .env written for
# Docker (DB_HOST=db, redis://redis:...) still works. 127.0.0.1 is used
# instead of "localhost", which can resolve to IPv6 on Windows.
export DB_HOST=127.0.0.1
export DB_PORT=5432
export DB_NAME=cansee
export DB_USER=postgres
export DB_PASSWORD=postgres
export REDIS_URL=redis://127.0.0.1:6379/0
export CELERY_BROKER_URL=redis://127.0.0.1:6379/1
# The optional intelligence/sources containers are not started; empty
# URLs make the backend run that logic in-process.
export INTELLIGENCE_SERVICE_URL=""
export SOURCES_SERVICE_URL=""

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
    {
      tail -n 30 "$LOG_DIR/migrate.log"
      if grep -q "already exists" "$LOG_DIR/migrate.log"; then
        echo
        echo "  The local database already has tables or indexes from an earlier"
        echo "  migration history (for example another branch or a local"
        echo "  makemigrations). If you do not need the local data, run:"
        echo "    ./run.sh reset-db"
        echo "  and then ./run.sh again."
      fi
      die "Migrations failed. Full log: $LOG_DIR/migrate.log"
    }
  ok "database up to date"

  # seed_prompts only exists on some branches.
  if [ -f "$BACKEND_DIR/apps/prompt_library/management/commands/seed_prompts.py" ]; then
    if manage seed_prompts >"$LOG_DIR/seed.log" 2>&1; then
      ok "prompt library seeded"
    else
      warn "Prompt library seeding failed (see $LOG_DIR/seed.log). The app still runs."
    fi
  fi

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
# Servers are tracked by the port they listen on and by whether they
# answer HTTP, not by process id: in Git Bash on Windows the shell's
# process ids do not reliably follow the native python/node processes,
# so a pid can look dead while the server is still running.

http_up() {
  local code
  code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

port_pids() {
  # Print the ids of processes listening on TCP port $1, one per line.
  local port="$1"
  if [ "$IS_WINDOWS" = "1" ]; then
    netstat -ano 2>/dev/null | tr -d '\r' |
      awk -v p=":$port" '$1 == "TCP" && $4 == "LISTENING" &&
        substr($2, length($2) - length(p) + 1) == p { print $5 }' | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
  fi
}

port_in_use() {
  local port="$1"
  [ -n "$(port_pids "$port")" ] && return 0
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return 0
  http_up "http://localhost:$port/"  # also catches IPv6-only listeners
}

kill_pid_tree() {
  # kill_pid_tree <pid>: on Windows a native pid, elsewhere a unix pid.
  local pid="$1"
  [ -n "$pid" ] || return 0
  if [ "$IS_WINDOWS" = "1" ]; then
    taskkill //F //T //PID "$pid" >/dev/null 2>&1 || true
  else
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
}

save_pid() {
  # Remember the launcher process (native pid on Windows) for clean stops.
  local name="$1" pid="$2"
  if [ "$IS_WINDOWS" = "1" ] && [ -r "/proc/$pid/winpid" ]; then pid="$(cat "/proc/$pid/winpid")"; fi
  echo "$pid" >"$RUN_DIR/$name.pid"
}

stop_server() {
  # stop_server <name> <port>
  local name="$1" port="$2" file="$RUN_DIR/$1.pid" pid i
  if [ -f "$file" ]; then
    read -r pid _ <"$file" || true
    kill_pid_tree "$pid"
    rm -f "$file"
  fi
  for pid in $(port_pids "$port"); do kill_pid_tree "$pid"; done
  for i in $(seq 1 10); do
    port_in_use "$port" || { ok "$name stopped"; return 0; }
    sleep 1
  done
  warn "$name: port $port is still in use. Close the program using it (process id: $(port_pids "$port" | tr '\n' ' '))."
}

free_port_or_die() {
  # free_port_or_die <name> <port>
  local name="$1" port="$2" answer
  port_in_use "$port" || return 0
  if [ -f "$RUN_DIR/$name.pid" ]; then
    echo "  A previous Cansee $name is still running on port $port; stopping it."
  else
    read -r -p "  Port $port is in use by another program. Stop it? [Y/n] " answer
    [[ "$answer" =~ ^[Nn]$ ]] && die "Port $port is in use. Free it and run again."
  fi
  stop_server "$name" "$port"
  port_in_use "$port" && die "Port $port is still in use."
  return 0
}

startup_failed() {
  # Known fatal startup messages, so a broken start fails fast.
  grep -qE "error when starting dev server|Error: That port is already in use|is already in use" "$1" 2>/dev/null
}

wait_for_http() {
  # wait_for_http <url> <seconds> <logfile> <label>
  local url="$1" timeout="$2" log="$3" label="$4" i
  for i in $(seq 1 "$timeout"); do
    if http_up "$url"; then
      ok "$label is up ($url)"
      return 0
    fi
    if startup_failed "$log"; then
      echo; tail -n 40 "$log"
      die "$label failed to start. Full log: $log"
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

# Byte offsets already printed from each log (read via ${!var}).
# shellcheck disable=SC2034
LOG_OFF_backend=0 LOG_OFF_frontend=0

show_new_log_lines() {
  # Print whatever was appended to <name>.log since the last call.
  local name="$1" file="$LOG_DIR/$1.log" var="LOG_OFF_$1" off size
  [ -f "$file" ] || return 0
  off="${!var}"
  size="$(wc -c <"$file" | tr -d '[:space:]')"
  [ "$size" -lt "$off" ] && off=0
  if [ "$size" -gt "$off" ]; then
    tail -c +"$((off + 1))" "$file" | head -c "$((size - off))" | sed "s/^/[$name] /"
    printf -v "$var" '%s' "$size"
  fi
}

check_proxy() {
  # The browser only talks to Vite; Vite forwards /api to Django. Ask an
  # API endpoint through Vite: Django answers 401 when nobody is signed
  # in, while a broken proxy answers 5xx (or nothing). This is the path
  # every login request takes.
  local code body="$RUN_DIR/proxy-check.txt"
  code="$(curl -s -o "$body" -m 15 -w '%{http_code}' "$UI_URL/api/v1/auth/session/" 2>/dev/null || true)"
  case "$code" in
    200 | 401 | 403)
      ok "Frontend reaches the backend (login requests will work)"
      ;;
    *)
      warn "The frontend cannot reach the backend through its /api proxy (HTTP ${code:-none}). Logins will fail."
      echo "  Response: $(head -c 300 "$body" 2>/dev/null | tr -d '\r')"
      echo "  Check $LOG_DIR/frontend.log for 'proxy error'."
      ;;
  esac
}

SERVERS_STARTED=0

cleanup() {
  trap - INT TERM EXIT
  [ "$SERVERS_STARTED" = "1" ] || return 0
  echo
  step "Shutting down servers"
  stop_server frontend "$UI_PORT"
  stop_server backend "$BACKEND_PORT"
  echo "  Postgres and Redis are still running in Docker (fast restart)."
  echo "  Run './run.sh stop' to stop them too."
}

on_signal() {
  cleanup
  exit 130
}

start_servers() {
  step "Starting the app"
  free_port_or_die backend "$BACKEND_PORT"
  free_port_or_die frontend "$UI_PORT"

  SERVERS_STARTED=1
  trap on_signal INT TERM
  trap cleanup EXIT

  # Outside Windows, give each server its own process group so the whole
  # tree (autoreloader, npm, vite) can be stopped together.
  [ "$IS_WINDOWS" = "1" ] || set -m
  (cd "$BACKEND_DIR" && exec "$(venv_python)" manage.py runserver "$BACKEND_HOST:$BACKEND_PORT") \
    >"$LOG_DIR/backend.log" 2>&1 </dev/null &
  save_pid backend "$!"

  # Point the Vite dev proxy at 127.0.0.1 explicitly: on Windows,
  # "localhost" can resolve to IPv6 (::1) while Django listens on IPv4.
  (cd "$UI_DIR" && VITE_API_URL="$BACKEND_URL" VITE_WS_URL="ws://$BACKEND_HOST:$BACKEND_PORT" \
    exec npm run dev -- --port "$UI_PORT" --strictPort) \
    >"$LOG_DIR/frontend.log" 2>&1 </dev/null &
  save_pid frontend "$!"
  [ "$IS_WINDOWS" = "1" ] || set +m

  wait_for_http "$BACKEND_URL/health/" 120 "$LOG_DIR/backend.log" "Backend"
  wait_for_http "$UI_URL/" 120 "$LOG_DIR/frontend.log" "Frontend"
  check_proxy

  open_browser "$UI_URL"

  cat <<EOF

${G}${B}Cansee is running.${N}

  App:        $UI_URL
  API:        $BACKEND_URL
  Logs:       $LOG_DIR
  New login:  ./users.sh     (in another Git Bash window)

Live logs follow below. Press Ctrl+C to stop the servers.

EOF

  # Follow both logs and watch both servers. A server counts as stopped
  # after ~15 seconds without an HTTP answer (Django's auto-reload after a
  # code change takes a few seconds, so single misses are ignored).
  local backend_miss=0 frontend_miss=0 tick=0
  while true; do
    show_new_log_lines backend
    show_new_log_lines frontend
    tick=$((tick + 1))
    if [ $((tick % 3)) = 0 ]; then
      if http_up "$BACKEND_URL/health/"; then backend_miss=0; else backend_miss=$((backend_miss + 1)); fi
      if http_up "$UI_URL/"; then frontend_miss=0; else frontend_miss=$((frontend_miss + 1)); fi
      if [ "$backend_miss" -ge 5 ]; then warn "The backend stopped responding. See $LOG_DIR/backend.log"; break; fi
      if [ "$frontend_miss" -ge 5 ]; then warn "The frontend stopped responding. See $LOG_DIR/frontend.log"; break; fi
    fi
    sleep 1
  done
  show_new_log_lines backend
  show_new_log_lines frontend
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
  # Prefer the dedicated user script when it sits next to this one.
  if [ -f "$SCRIPT_DIR/users.sh" ]; then
    exec bash "$SCRIPT_DIR/users.sh" add
  fi
  mkdir -p "$LOG_DIR"
  [ -d "$BACKEND_DIR" ] || die "Backend not found at $BACKEND_DIR. Run ./run.sh setup first."
  [ -f "$(venv_python)" ] || die "Backend is not installed yet. Run ./run.sh setup first."
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop."
  start_services
  create_user
}

cmd_reset_db() {
  step "Reset the local database"
  [ -d "$BACKEND_DIR" ] || die "Backend not found at $BACKEND_DIR."
  docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop."
  echo "  This permanently deletes the local development database (logins,"
  echo "  projects, audits). Your code, .env and production are not touched."
  local answer
  read -r -p "  Type 'reset' to continue: " answer
  [ "$answer" = "reset" ] || { echo "  Cancelled."; return 0; }
  stop_server frontend "$UI_PORT"
  stop_server backend "$BACKEND_PORT"
  compose down -v >/dev/null 2>&1 || die "Could not remove the database container and volume."
  ok "Local database deleted. Run ./run.sh to recreate it and create a login."
}

cmd_stop() {
  step "Stopping Cansee"
  stop_server frontend "$UI_PORT"
  stop_server backend "$BACKEND_PORT"
  if [ -d "$BACKEND_DIR" ] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    compose stop db redis >/dev/null 2>&1 || true
    ok "Postgres and Redis stopped (data is kept)"
  fi
}

cmd_status() {
  step "Status"
  if http_up "$BACKEND_URL/health/"; then ok "backend running ($BACKEND_URL)"; else echo "  backend not running"; fi
  if http_up "$UI_URL/"; then ok "frontend running ($UI_URL)"; else echo "  frontend not running"; fi
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
      start | setup | user | stop | status | logs | reset-db) cmd="$1" ;;
      *) usage; die "Unknown command or option: $1" ;;
    esac
    shift
  done

  case "$cmd" in
    start) cmd_start ;;
    setup) cmd_setup ;;
    user) cmd_user ;;
    stop) cmd_stop ;;
    reset-db) cmd_reset_db ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
  esac
}

main "$@"
