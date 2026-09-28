#!/usr/bin/env bash
set -Eeuo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing .env (copy .env.example first).' >&2; exit 1; }
set -a; . ./.env; set +a
: "${QUICKLY_BOOTSTRAP_ADMIN_USERNAME:?missing bootstrap username}"
: "${QUICKLY_BOOTSTRAP_ADMIN_EMAIL:?missing bootstrap email}"
: "${QUICKLY_BOOTSTRAP_ADMIN_PASSWORD:?missing bootstrap password}"
# The API expects JSON. Limit these bootstrap values to the URL-safe alphabet so
# shell/JSON quoting cannot accidentally change a credential in transit.
[[ "$QUICKLY_BOOTSTRAP_ADMIN_USERNAME" =~ ^[A-Za-z0-9_-]+$ ]] || { echo 'Bootstrap username must be alphanumeric, _ or -.' >&2; exit 1; }
[[ "$QUICKLY_BOOTSTRAP_ADMIN_PASSWORD" =~ ^[A-Za-z0-9_-]+$ ]] || { echo 'Use a URL-safe bootstrap password (letters, digits, _ and -).' >&2; exit 1; }

endpoint=http://127.0.0.1:19080/api/auth/setup-status
status=$(curl --fail --silent --show-error "$endpoint")
if grep -q '"setup_complete":true' <<<"$status"; then
  echo 'Initial admin already exists; registration remains closed.'
  exit 0
fi
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:19080/api/auth/register \
  --data "$(printf '{\"username\":\"%s\",\"email\":\"%s\",\"password\":\"%s\"}' "$QUICKLY_BOOTSTRAP_ADMIN_USERNAME" "$QUICKLY_BOOTSTRAP_ADMIN_EMAIL" "$QUICKLY_BOOTSTRAP_ADMIN_PASSWORD")" >/dev/null
echo 'Initial Quickly admin created; remove QUICKLY_BOOTSTRAP_ADMIN_* from .env now.'
