#!/usr/bin/env bash
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

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
