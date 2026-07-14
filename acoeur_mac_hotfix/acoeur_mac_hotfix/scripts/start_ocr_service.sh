#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$HOME/acoeur-ocr"

cd "$PROJECT_DIR"

if [ ! -d "$VENV_DIR" ]; then
  echo "[Acoeur OCR] Creating Python venv..."
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip setuptools wheel

if python - <<'PY'
import importlib.util

required = ["paddleocr", "paddle", "fitz"]
missing = [name for name in required if importlib.util.find_spec(name) is None]

if missing:
    print("[Acoeur OCR] Missing:", missing)
    raise SystemExit(1)

print("[Acoeur OCR] Dependencies already installed.")
PY
then
  echo "[Acoeur OCR] Dependency check passed."
else
  echo "[Acoeur OCR] Installing OCR dependencies..."
  python -m pip install paddlepaddle==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
  python -m pip install paddleocr==3.6.0
  python -m pip install "PyMuPDF==1.26.5"
fi

export ACOEUR_HOST_MEDIA_ROOT="$PROJECT_DIR/media"
export ACOEUR_CONTAINER_MEDIA_ROOT="/app/media"
export ACOEUR_OCR_HOST="127.0.0.1"
export ACOEUR_OCR_PORT="8765"
export ACOEUR_OCR_LANG="fr"

python "$PROJECT_DIR/scripts/ocr_service.py"
