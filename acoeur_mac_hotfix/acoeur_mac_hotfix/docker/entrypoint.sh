#!/usr/bin/env bash
set -e

echo "[Acoeur] Waiting for database..."

python - <<'PY'
import os
import time
import psycopg

host = os.getenv("POSTGRES_HOST", "db")
port = int(os.getenv("POSTGRES_PORT", "5432"))
dbname = os.getenv("POSTGRES_DB", "hospital_orders")
user = os.getenv("POSTGRES_USER", "hospital_user")
password = os.getenv("POSTGRES_PASSWORD", "hospital_password")

for i in range(60):
    try:
        conn = psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=3,
        )
        conn.close()
        print("[Acoeur] Database is ready.")
        break
    except Exception as exc:
        print(f"[Acoeur] DB not ready yet ({i+1}/60): {exc}")
        time.sleep(2)
else:
    raise SystemExit("[Acoeur] Database is not available.")
PY

echo "[Acoeur] Running migrations..."
python manage.py migrate --noinput

echo "[Acoeur] Collecting static files..."
python manage.py collectstatic --noinput

echo "[Acoeur] Starting Django..."
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 2 \
    --timeout 1200 \
    --graceful-timeout 1200 \
    --max-requests 50 \
    --max-requests-jitter 10 \
    --access-logfile - \
    --error-logfile - \
    --capture-output
