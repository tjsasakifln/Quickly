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
cp .env "$work/runtime.env"
printf '%s\n' "$QUICKLY_IMAGE" >"$work/image.txt"
tar -C "$work" -czf "backups/quickly-confenge-$stamp.tgz" database.dump runtime.env image.txt
chmod 600 "backups/quickly-confenge-$stamp.tgz"
find backups -type f -name 'quickly-confenge-*.tgz' -mtime +30 -delete
echo "Created backups/quickly-confenge-$stamp.tgz"
