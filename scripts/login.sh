#!/usr/bin/env bash
#
# Interactive login helper.
#
# Prompts for email + password, hits the auth endpoint, prints the
# JWT access token (and the user object). Useful for poking the API
# with curl as a real user without typing the full login payload.
#
# Usage:
#   scripts/login.sh                       # prompts for env + creds
#   scripts/login.sh prod                  # skip env prompt
#   scripts/login.sh local me@x.com pw     # skip everything, just run
#
# After a successful login the access token is also stashed at
# /tmp/fb_access_token so follow-up curls can do:
#     curl -H "Authorization: Bearer $(cat /tmp/fb_access_token)" ...

set -euo pipefail

# ── Style ────────────────────────────────────────────────────────────
B=$'\033[1m'; G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'
C=$'\033[0;36m'; D=$'\033[2m'; N=$'\033[0m'

# ── Env config ───────────────────────────────────────────────────────
env_choice="${1:-}"
email_arg="${2:-}"
password_arg="${3:-}"

if [[ -z "$env_choice" ]]; then
  printf "${B}Where do you want to log in?${N}\n"
  printf "  ${G}1${N} local  (http://localhost:8000)\n"
  printf "  ${G}2${N} prod   (https://cansee.ai)\n"
  printf "Pick [1/2, default 1]: "
  read -r pick
  case "$pick" in
    2|prod)  env_choice="prod" ;;
    *)       env_choice="local" ;;
  esac
fi

case "$env_choice" in
  local)
    base_url="${BASE_URL:-http://localhost:8000}"
    frontend_base="${FRONTEND_BASE:-http://localhost:5173}"
    ;;
  prod)
    base_url="${BASE_URL:-https://cansee.ai}"
    frontend_base="${FRONTEND_BASE:-https://cansee.ai}"
    ;;
  *)
    base_url="$env_choice"  # allow a full URL override
    frontend_base="${FRONTEND_BASE:-$env_choice}"
    ;;
esac

# ── Prompt for credentials ───────────────────────────────────────────
if [[ -z "$email_arg" ]]; then
  printf "${B}Email:${N} "
  read -r email_arg
fi
[[ -z "$email_arg" ]] && { printf "${R}Email is required.${N}\n" >&2; exit 1; }

if [[ -z "$password_arg" ]]; then
  printf "${B}Password:${N} "
  # Read silently so the password doesn't show in the terminal or in
  # shell history. stty only works on a real terminal; piped stdin
  # (scripts, CI) falls back to a plain read.
  if [[ -t 0 ]]; then
    stty -echo
    read -r password_arg
    stty echo
    printf "\n"
  else
    read -r password_arg
    printf "\n"
  fi
fi
[[ -z "$password_arg" ]] && { printf "${R}Password is required.${N}\n" >&2; exit 1; }

# ── Hit the login endpoint ───────────────────────────────────────────
printf "\n${C}→ POST %s/api/v1/auth/login/${N}\n" "$base_url"

# Build the JSON body without echoing the password to the args (which
# would show up in 'ps').
tmp_body=$(mktemp)
trap 'rm -f "$tmp_body"' EXIT
# Use python for safe JSON encoding of the password so quotes/specials
# don't break the request.
python3 - "$email_arg" "$password_arg" > "$tmp_body" <<'PY'
import json, sys
print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))
PY

response=$(curl -s -w "\n%{http_code}" \
  -X POST "$base_url/api/v1/auth/login/" \
  -H "Content-Type: application/json" \
  --data-binary "@$tmp_body") \
  || { printf "${R}✗ Could not reach %s — is the server up?${N}\n" "$base_url" >&2; exit 1; }

status="${response##*$'\n'}"
body="${response%$'\n'*}"

# ── Render ───────────────────────────────────────────────────────────
if [[ "$status" == "200" ]]; then
  access=$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    inner = d.get("data") or d
    print(inner.get("access", ""))
except Exception:
    pass
')
  user=$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    inner = d.get("data") or d
    u = inner.get("user", {})
    print(f"{u.get(\"full_name\",\"\")} <{u.get(\"email\",\"\")}>  id={u.get(\"id\",\"\")}  plan={u.get(\"plan\",\"\")}")
except Exception:
    pass
')

  printf "${G}✓ Login OK${N}  ${D}(%s)${N}\n" "$status"
  [[ -n "$user" ]]   && printf "${B}User:${N}   %s\n" "$user"
  if [[ -n "$access" ]]; then
    printf "${B}Token:${N}  %s%s%s\n" "${access:0:32}" "…" "${access: -8}"
    printf '%s' "$access" > /tmp/fb_access_token
    printf "${D}(full token saved to /tmp/fb_access_token)${N}\n\n"
    printf "${B}Use it in follow-up calls:${N}\n"
    printf "  ${D}curl -H \"Authorization: Bearer \$(cat /tmp/fb_access_token)\" %s/api/v1/auth/me/${N}\n" "$base_url"
    printf "\n${B}Or open the app already signed in:${N}\n"
    printf "  %s/login?auto_token=%s\n" "$frontend_base" "$access"
    printf "${D}(auto-token sessions last until the access token expires;${N}\n"
    printf "${D} no refresh cookie is set by this path)${N}\n"
  fi
  exit 0
fi

printf "${R}✗ Login failed${N}  ${D}(%s)${N}\n" "$status"
printf '%s\n' "$body" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$body"

# Friendly suggestion when the user doesn't exist yet.
if printf '%s' "$body" | grep -qiE "invalid email or password|validation_error"; then
  printf "\n${Y}Tip:${N} if this user doesn't exist, create them with:\n"
  if [[ "$env_choice" == "prod" ]]; then
    printf "  ${D}scripts/run.sh prod-create-user %s '%s'${N}\n" "$email_arg" "Full Name"
  else
    printf "  ${D}scripts/run.sh create-user %s '%s'${N}\n" "$email_arg" "Full Name"
  fi
fi
exit 1
