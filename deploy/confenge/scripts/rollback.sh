#!/usr/bin/env bash
set -Eeuo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f compose.yml.previous && test -f .env.previous || { echo 'No update snapshot available.' >&2; exit 1; }
cp compose.yml.previous compose.yml
cp .env.previous .env
chmod 600 .env
docker compose -f compose.yml up -d --wait
curl --fail --silent --show-error http://127.0.0.1:19080/api/auth/setup-status >/dev/null
echo 'Rollback completed. Database was not downgraded; restore a compatible backup if the target release requires it.'
