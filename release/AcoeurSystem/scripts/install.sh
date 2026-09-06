#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "[Acoeur] Checking Docker..."

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker command not found. Please install Docker Desktop first."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running. Please open Docker Desktop first."
  exit 1
fi

mkdir -p media outputs data backups

if [ ! -f .env ]; then
  echo
  echo "[Acoeur] Creating .env"
  echo "Please enter this Mac's Tailscale IPv4 address."
  echo "Example: 100.xx.xx.xx"
  echo "If you do not know it yet, press Enter. You can edit .env later."
  echo
  read -r -p "Tailscale IPv4: " TAILSCALE_IP

  SECRET_KEY="$(uuidgen)-$(uuidgen)"
  ALLOWED_HOSTS="127.0.0.1,localhost,0.0.0.0"

  if [ -n "$TAILSCALE_IP" ]; then
    ALLOWED_HOSTS="$ALLOWED_HOSTS,$TAILSCALE_IP"
  fi

  cat > .env <<ENVEOF
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=$SECRET_KEY
DJANGO_ALLOWED_HOSTS=$ALLOWED_HOSTS

POSTGRES_DB=hospital_orders
POSTGRES_USER=hospital_user
POSTGRES_PASSWORD=hospital_password
POSTGRES_HOST=db
POSTGRES_PORT=5432
ENVEOF

  echo "[Acoeur] .env created."
fi

echo "[Acoeur] Building Docker image. This may take a long time on first install..."
docker compose build web

echo "[Acoeur] Starting PostgreSQL..."
docker compose up -d db

echo "[Acoeur] Waiting for PostgreSQL..."
for i in {1..60}; do
  if docker compose exec -T db pg_isready -U hospital_user -d hospital_orders >/dev/null 2>&1; then
    echo "[Acoeur] PostgreSQL is ready."
    break
  fi

  if [ "$i" -eq 60 ]; then
    echo "[Acoeur] PostgreSQL did not become ready."
    exit 1
  fi

  sleep 2
done

if [ -f initial_data/initial_db.dump ] && [ ! -f .initial_data_restored ]; then
  echo "[Acoeur] Restoring initial database..."
  cat initial_data/initial_db.dump | docker compose exec -T db pg_restore \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    -U hospital_user \
    -d hospital_orders

  touch .initial_data_restored
  echo "[Acoeur] Database restored."
fi

if [ -f initial_data/media.tar.gz ] && [ ! -f .initial_media_restored ]; then
  echo "[Acoeur] Restoring media files..."
  tar -xzf initial_data/media.tar.gz
  touch .initial_media_restored
  echo "[Acoeur] Media restored."
fi

if [ -f initial_data/outputs.tar.gz ] && [ ! -f .initial_outputs_restored ]; then
  echo "[Acoeur] Restoring outputs files..."
  tar -xzf initial_data/outputs.tar.gz
  touch .initial_outputs_restored
  echo "[Acoeur] Outputs restored."
fi

echo "[Acoeur] Starting web service..."
docker compose up -d web

echo
echo "[Acoeur] Installation finished."
echo "Open: http://localhost:8000/admin/"
echo

open http://localhost:8000/admin/
