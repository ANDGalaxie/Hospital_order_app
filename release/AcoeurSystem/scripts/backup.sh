#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

mkdir -p backups

TS="$(date +%Y%m%d_%H%M%S)"

echo "[Acoeur] Backing up database..."
docker compose exec -T db pg_dump \
  -U hospital_user \
  -d hospital_orders \
  -Fc \
  --no-owner \
  --no-privileges \
  > "backups/db_$TS.dump"

echo "[Acoeur] Backing up media and outputs..."
tar -czf "backups/files_$TS.tar.gz" media outputs 2>/dev/null || true

echo "[Acoeur] Backup finished:"
echo "backups/db_$TS.dump"
echo "backups/files_$TS.tar.gz"
