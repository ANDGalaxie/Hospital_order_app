#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

docker compose up -d
echo "Acoeur started."
open http://localhost:8000/admin/
