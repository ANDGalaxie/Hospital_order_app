#!/usr/bin/env bash
set -eu

echo "[Acoeur release] Waiting for the configured database..."
python - <<'PY'
import os
import time

import django
from django.db import connections
from django.db.utils import OperationalError

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

for attempt in range(1, 61):
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
        print("[Acoeur release] Database is ready.")
        break
    except OperationalError:
        connections["default"].close()
        print(f"[Acoeur release] Database not ready ({attempt}/60).")
        time.sleep(2)
else:
    raise SystemExit("Configured database is unavailable.")
PY

python manage.py check --deploy
python manage.py migrate --noinput
echo "[Acoeur release] Migrations complete."
