#!/usr/bin/env bash
set -Eeuo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing .env.' >&2; exit 1; }
grep -Eq '^QUICKLY_IMAGE=ghcr\.io/tjsasakifln/quickly-confenge@sha256:[0-9a-f]{64}$' .env || { echo 'Refusing image outside the approved Confenge GHCR digest.' >&2; exit 1; }
./scripts/backup.sh
cp compose.yml compose.yml.previous
cp .env .env.previous
docker compose -f compose.yml pull app
docker compose -f compose.yml up -d --wait
curl --fail --silent --show-error http://127.0.0.1:19080/api/auth/setup-status >/dev/null
echo 'Pinned Confenge image is running; previous configuration retained as *.previous.'
