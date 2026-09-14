#!/usr/bin/env bash
# Copy the vault out of the data volume; keep the last 14 copies.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
docker compose exec -T app python -c "import sqlite3; src=sqlite3.connect('/data/vault.db'); dst=sqlite3.connect('/data/vault-backup.db'); src.backup(dst); dst.close()"
docker compose cp app:/data/vault-backup.db "backups/vault-$(date +%F-%H%M).db"
ls -1t backups/vault-*.db | tail -n +15 | xargs -r rm --
echo "backups:"; ls -1 backups
