#!/usr/bin/env bash
set -Eeuo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing .env.' >&2; exit 1; }

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 ghcr.io/tjsasakifln/quickly-confenge@sha256:<digest>" >&2
  exit 2
fi

target_image=$1
[[ "$target_image" =~ ^ghcr\.io/tjsasakifln/quickly-confenge@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Refusing image outside the approved Confenge GHCR digest.' >&2
  exit 1
}

current_image=$(sed -n 's/^QUICKLY_IMAGE=//p' .env)
[[ "$current_image" =~ ^ghcr\.io/tjsasakifln/quickly-confenge@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Current QUICKLY_IMAGE is not an approved Confenge GHCR digest.' >&2
  exit 1
}

if [[ "$target_image" == "$current_image" ]]; then
  echo 'Target image is already configured; nothing to update.'
  exit 0
fi

# Pull first so an unavailable target cannot change the local configuration.
docker pull "$target_image"

# The backup and *.previous snapshots must describe the currently running
# release, before QUICKLY_IMAGE is changed to the target digest.
./scripts/backup.sh
cp compose.yml compose.yml.previous
cp .env .env.previous

new_env=$(mktemp -p . .env.update.XXXXXX)
trap 'rm -f "$new_env"' EXIT
awk -v image="$target_image" '
  BEGIN { replaced = 0 }
  /^QUICKLY_IMAGE=/ { print "QUICKLY_IMAGE=" image; replaced = 1; next }
  { print }
  END { if (!replaced) exit 1 }
' .env >"$new_env"
chmod 600 "$new_env"
mv "$new_env" .env

docker compose -f compose.yml up -d --wait
curl --fail --silent --show-error http://127.0.0.1:19080/api/auth/setup-status >/dev/null
echo 'Pinned Confenge image is running; previous configuration retained as *.previous.'
