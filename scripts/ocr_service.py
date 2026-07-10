#!/usr/bin/env python3
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fitz  # PyMuPDF
from paddleocr import PaddleOCR


HOST = os.getenv("ACOEUR_OCR_HOST", "127.0.0.1")
PORT = int(os.getenv("ACOEUR_OCR_PORT", "8765"))

CONTAINER_MEDIA_ROOT = os.getenv("ACOEUR_CONTAINER_MEDIA_ROOT", "/app/media")
HOST_MEDIA_ROOT = os.getenv(
    "ACOEUR_HOST_MEDIA_ROOT",
    str(Path.home() / "AcoeurSystem" / "media"),
)

OCR_LANG = os.getenv("ACOEUR_OCR_LANG", "fr")
OCR_ZOOM = float(os.getenv("ACOEUR_OCR_ZOOM", "2.0"))

_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        print(f"[OCR SERVICE] Loading PaddleOCR lang={OCR_LANG} ...", flush=True)
        _ocr = PaddleOCR(lang=OCR_LANG)
        print("[OCR SERVICE] PaddleOCR loaded.", flush=True)
    return _ocr


def map_container_path(path_text: str) -> Path:
    path_text = str(path_text)

    if path_text == CONTAINER_MEDIA_ROOT:
        return Path(HOST_MEDIA_ROOT)

    prefix = CONTAINER_MEDIA_ROOT.rstrip("/") + "/"
    if path_text.startswith(prefix):
        relative = path_text[len(prefix):]
        return Path(HOST_MEDIA_ROOT) / relative

    return Path(path_text)


def pdf_to_images(pdf_path: Path, ocr_dir: Path):
    print(f"[OCR SERVICE] PDF to images: {pdf_path}", flush=True)

    image_paths = []
    doc = fitz.open(str(pdf_path))

    for index, page in enumerate(doc, start=1):
        image_path = ocr_dir / f"page_{index}.png"
        matrix = fitz.Matrix(OCR_ZOOM, OCR_ZOOM)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(image_path))
        image_paths.append(image_path)

    doc.close()
    print(f"[OCR SERVICE] Converted {len(image_paths)} pages.", flush=True)
    return image_paths


def run_ocr(pdf_path_text: str, ocr_dir_text: str, force_ocr: bool = False):
    pdf_path = map_container_path(pdf_path_text)
    ocr_dir = map_container_path(ocr_dir_text)

    print("[OCR SERVICE] Request received.", flush=True)
    print(f"[OCR SERVICE] pdf_path={pdf_path}", flush=True)
    print(f"[OCR SERVICE] ocr_dir={ocr_dir}", flush=True)
    print(f"[OCR SERVICE] force_ocr={force_ocr}", flush=True)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    ocr_dir.mkdir(parents=True, exist_ok=True)

    existing_jsons = sorted(ocr_dir.glob("*_res.json"))
    if existing_jsons and not force_ocr:
        print(f"[OCR SERVICE] Existing OCR JSON found, skip OCR: {ocr_dir}", flush=True)
        return {
            "ok": True,
            "skipped": True,
            "message": "Existing OCR JSON found, skipped.",
            "ocr_dir": str(ocr_dir),
            "json_count": len(existing_jsons),
        }

    image_paths = pdf_to_images(pdf_path, ocr_dir)

    ocr = get_ocr()

    json_count = 0
    image_count = 0

    for image_path in image_paths:
        print(f"[OCR SERVICE] OCR image: {image_path}", flush=True)
        result = ocr.predict(str(image_path))

        for res in result:
            if hasattr(res, "save_to_json"):
                res.save_to_json(save_path=str(ocr_dir))
                json_count += 1

            if hasattr(res, "save_to_img"):
                res.save_to_img(save_path=str(ocr_dir))
                image_count += 1

    print(f"[OCR SERVICE] OCR finished. json_count={json_count}", flush=True)

    return {
        "ok": True,
        "skipped": False,
        "message": "OCR finished.",
        "ocr_dir": str(ocr_dir),
        "json_count": json_count,
        "image_count": image_count,
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "acoeur-ocr"})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        if self.path != "/ocr":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))

            result = run_ocr(
                pdf_path_text=payload["pdf_path"],
                ocr_dir_text=payload["ocr_dir"],
                force_ocr=bool(payload.get("force_ocr", False)),
            )

            self._send_json(200, result)

        except Exception as exc:
            print("[OCR SERVICE] ERROR:", flush=True)
            traceback.print_exc()

            self._send_json(
                500,
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[OCR SERVICE] Listening on http://{HOST}:{PORT}", flush=True)
    print(f"[OCR SERVICE] HOST_MEDIA_ROOT={HOST_MEDIA_ROOT}", flush=True)
    print(f"[OCR SERVICE] CONTAINER_MEDIA_ROOT={CONTAINER_MEDIA_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
