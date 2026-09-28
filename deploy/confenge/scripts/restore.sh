#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -ne 1 ]; then echo "Usage: $0 backups/quickly-confenge-YYYYmmddTHHMMSSZ.tgz" >&2; exit 2; fi
archive=$1
test -f "$archive" || { echo "Backup not found: $archive" >&2; exit 1; }
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root_dir"
test -f .env || { echo 'Missing current .env.' >&2; exit 1; }
set -a; . ./.env; set +a
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
tar -xzf "$archive" -C "$work"
test -s "$work/database.dump" || { echo 'Archive has no database dump.' >&2; exit 1; }
test -s "$work/runtime.env" || { echo 'Archive has no runtime environment.' >&2; exit 1; }
# A different encryption key makes SMTP/IMAP passwords irrecoverable. Refuse a
# surprising restore; for a full host recovery, first place runtime.env at .env
# and start this stack, then invoke this script.
archived_key=$(grep '^QUICKLY_ENCRYPTION_KEY=' "$work/runtime.env" || true)
current_key=$(grep '^QUICKLY_ENCRYPTION_KEY=' .env || true)
[ "$archived_key" = "$current_key" ] || { echo 'Encryption key differs. For full recovery securely restore runtime.env as .env before restoring the database.' >&2; exit 1; }
docker compose -f compose.yml stop app
docker compose -f compose.yml exec -T db dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker compose -f compose.yml exec -T db createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose -f compose.yml exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error <"$work/database.dump"
echo 'Database restored using the matching stable encryption key.'
docker compose -f compose.yml up -d app
