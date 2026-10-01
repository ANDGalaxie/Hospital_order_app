import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from scripts import ocr_service


class OCRServiceTests(SimpleTestCase):
    def test_get_ocr_disables_mkldnn(self):
        sentinel = object()

        with patch.object(ocr_service, "_ocr", None), patch.object(
            ocr_service, "PaddleOCR", return_value=sentinel
        ) as paddle_ocr:
            self.assertIs(ocr_service.get_ocr(), sentinel)

        paddle_ocr.assert_called_once_with(
            lang=ocr_service.OCR_LANG, enable_mkldnn=False
        )

    def test_health_endpoint_returns_ready_response(self):
        server = HTTPServer(("127.0.0.1", 0), ocr_service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/health", timeout=5
        ) as response:
            payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"ok": True, "service": "acoeur-ocr"})

    def test_main_initializes_model_before_opening_health_port(self):
        events = []
        fake_server = Mock()
        fake_server.serve_forever.side_effect = lambda: events.append("serve")

        with patch.object(
            ocr_service, "get_ocr", side_effect=lambda: events.append("model")
        ), patch.object(
            ocr_service,
            "HTTPServer",
            side_effect=lambda *args: events.append("server") or fake_server,
        ):
            ocr_service.main()

        self.assertEqual(events, ["model", "server", "serve"])

    def test_path_mapping_rejects_traversal_and_external_paths(self):
        with patch.object(ocr_service, "CONTAINER_MEDIA_ROOT", "/app/media"), patch.object(
            ocr_service, "HOST_MEDIA_ROOT", "/tmp/acoeurs-ocr-test-media"
        ):
            mapped = ocr_service.map_container_path("/app/media/orders/input.pdf")
            self.assertEqual(mapped, Path("/tmp/acoeurs-ocr-test-media/orders/input.pdf"))

            for path in ("/etc/passwd", "/app/media/../../etc/passwd"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    ocr_service.map_container_path(path)

    def test_error_response_does_not_expose_exception_or_traceback(self):
        server = HTTPServer(("127.0.0.1", 0), ocr_service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/ocr",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(ocr_service.traceback, "print_exc"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
        payload = json.loads(raised.exception.read())

        self.assertEqual(payload["error"], "OCR processing failed. Consult service logs.")
        self.assertNotIn("traceback", payload)

