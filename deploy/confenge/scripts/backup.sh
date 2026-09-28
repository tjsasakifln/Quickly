#!/usr/bin/env bash
set -Eeuo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing .env.' >&2; exit 1; }
set -a; . ./.env; set +a
umask 077
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
docker compose -f compose.yml exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc >"$work/database.dump"
printf '%s\n' "$QUICKLY_IMAGE" >"$work/image.txt"
printf '%s\n' "$(printf '%s' "$QUICKLY_ENCRYPTION_KEY" | sha256sum | awk '{print $1}')" >"$work/encryption-key.sha256"
archive="backups/quickly-confenge-$stamp.tgz"
tar -C "$work" -czf "$archive" database.dump image.txt encryption-key.sha256
(cd "$(dirname "$archive")" && sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256")
chmod 600 "$archive" "$archive.sha256"
find backups -type f -name 'quickly-confenge-*.tgz' -mtime +30 -delete
find backups -type f -name 'quickly-confenge-*.tgz.sha256' -mtime +30 -delete
echo "Created $archive and $archive.sha256. Copy both to encrypted offsite storage; this local backup is not the only copy."
