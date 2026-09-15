#!/usr/bin/env bash
# Copy the vault out of the data volume into a private directory outside the checkout; keep the last 14 copies.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
BACKUP_DIR="${BACKUP_DIR:-$HOME/chat-johnson-backups}"
mkdir -p "${BACKUP_DIR}"
docker compose exec -T app python -c "import sqlite3; src=sqlite3.connect('/data/vault.db'); dst=sqlite3.connect('/data/vault-backup.db'); src.backup(dst); dst.close()"
docker compose cp app:/data/vault-backup.db "${BACKUP_DIR}/vault-$(date +%F-%H%M).db"
docker compose exec -T app rm -f /data/vault-backup.db
chmod 600 "${BACKUP_DIR}"/vault-*.db
ls -1t "${BACKUP_DIR}"/vault-*.db | tail -n +15 | xargs -r rm --
echo "backups in ${BACKUP_DIR}:"; ls -1 "${BACKUP_DIR}"
