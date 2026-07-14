#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

echo "[Acoeur] Backup before update..."
./scripts/backup.sh

echo "[Acoeur] Rebuilding web image..."
docker compose build --no-cache web

echo "[Acoeur] Restarting services..."
docker compose up -d

echo "[Acoeur] Update finished."
open http://localhost:8000/admin/
