#!/usr/bin/env bash
set -Eeuo pipefail

allow_image_mismatch=false
if [ "${1:-}" = "--allow-image-mismatch" ]; then
  allow_image_mismatch=true
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [--allow-image-mismatch] backups/quickly-confenge-YYYYmmddTHHMMSSZ.tgz" >&2
  exit 2
fi
archive=$1
test -f "$archive" || { echo "Backup not found: $archive" >&2; exit 1; }
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing current .env.' >&2; exit 1; }
set -a; . ./.env; set +a
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
if [ -f "$archive.sha256" ]; then
  (cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256") || { echo 'Backup checksum verification failed; restore aborted.' >&2; exit 1; }
else
  echo 'Backup checksum sidecar is missing; restore aborted.' >&2
  exit 1
fi
tar -xzf "$archive" -C "$work"
test -s "$work/database.dump" || { echo 'Archive has no database dump.' >&2; exit 1; }
test -s "$work/image.txt" || { echo 'Archive has no image reference.' >&2; exit 1; }
test -s "$work/encryption-key.sha256" || { echo 'Archive has no encryption-key fingerprint.' >&2; exit 1; }
archived_image=$(tr -d '\r\n' <"$work/image.txt")
current_image=${QUICKLY_IMAGE:-}
if [ "$archived_image" != "$current_image" ] && [ "$allow_image_mismatch" != true ]; then
  echo 'Backup image digest differs from current QUICKLY_IMAGE. Restore the matching approved digest first, or explicitly pass --allow-image-mismatch after compatibility review.' >&2
  exit 1
fi
archived_key_fingerprint=$(tr -d '\r\n' <"$work/encryption-key.sha256")
current_key_fingerprint=$(printf '%s' "$QUICKLY_ENCRYPTION_KEY" | sha256sum | awk '{print $1}')
[ "$archived_key_fingerprint" = "$current_key_fingerprint" ] || { echo 'Encryption-key fingerprint differs; restore aborted because mailbox credentials would not be decryptable.' >&2; exit 1; }
docker compose -f compose.yml stop app
docker compose -f compose.yml exec -T db dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker compose -f compose.yml exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose -f compose.yml exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error <"$work/database.dump"
echo 'Database restored using the matching stable encryption key and approved image digest.'
docker compose -f compose.yml up -d app
