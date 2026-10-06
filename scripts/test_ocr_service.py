import json
import os
import runpy
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from django.test import SimpleTestCase

from scripts import ocr_service


class OCRServiceTests(SimpleTestCase):
    def test_detector_limits_default_to_max_and_960(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ocr_service.read_detector_limits(), ("max", 960))
            state = runpy.run_path(ocr_service.__file__)
        self.assertEqual(state["OCR_DET_LIMIT_TYPE"], "max")
        self.assertEqual(state["OCR_DET_LIMIT_SIDE_LEN"], 960)

    def test_detector_limits_accept_valid_environment_overrides(self):
        for limit_type in ("max", "min", "resize_long"):
            with self.subTest(limit_type=limit_type), patch.dict(os.environ, {
                "ACOEUR_OCR_DET_LIMIT_TYPE": f" {limit_type} ",
                "ACOEUR_OCR_DET_LIMIT_SIDE_LEN": " 640 ",
            }):
                self.assertEqual(ocr_service.read_detector_limits(), (limit_type, 640))

    def test_detector_limits_reject_invalid_type(self):
        for value in ("", "MAX", "unsupported"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "ACOEUR_OCR_DET_LIMIT_TYPE": value,
                "ACOEUR_OCR_DET_LIMIT_SIDE_LEN": "960",
            }):
                with self.assertRaisesRegex(ValueError, "ACOEUR_OCR_DET_LIMIT_TYPE"):
                    ocr_service.read_detector_limits()

    def test_detector_limits_reject_invalid_or_nonpositive_side_length(self):
        for value in ("", "invalid", "960.5", "NaN", "inf", "0", "-960"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "ACOEUR_OCR_DET_LIMIT_TYPE": "max",
                "ACOEUR_OCR_DET_LIMIT_SIDE_LEN": value,
            }):
                with self.assertRaisesRegex(ValueError, "must be a positive integer"):
                    ocr_service.read_detector_limits()

    def test_invalid_detector_settings_fail_before_model_or_health_server_start(self):
        for values in (
            {"ACOEUR_OCR_DET_LIMIT_SIDE_LEN": "0"},
            {"ACOEUR_OCR_DET_LIMIT_SIDE_LEN": "-1"},
            {"ACOEUR_OCR_DET_LIMIT_SIDE_LEN": "invalid"},
            {"ACOEUR_OCR_DET_LIMIT_TYPE": "unsupported"},
        ):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with patch("paddleocr.PaddleOCR") as model, patch("http.server.HTTPServer") as server:
                    with self.assertRaisesRegex(ValueError, "ACOEUR_OCR_DET_LIMIT_"):
                        runpy.run_path(ocr_service.__file__, run_name="__main__")
                model.assert_not_called()
                server.assert_not_called()

    def test_effective_detector_limits_are_logged_once_at_initialization(self):
        sentinel = object()
        with patch.object(ocr_service, "_ocr", None), patch.object(
            ocr_service, "PaddleOCR", return_value=sentinel
        ) as model, patch.object(ocr_service, "OCR_DET_LIMIT_TYPE", "max"), patch.object(
            ocr_service, "OCR_DET_LIMIT_SIDE_LEN", 640
        ), patch("builtins.print") as output:
            self.assertIs(ocr_service.get_ocr(), sentinel)
            self.assertIs(ocr_service.get_ocr(), sentinel)
        model.assert_called_once_with(lang=ocr_service.OCR_LANG, enable_mkldnn=False)
        config_logs = [
            entry.args[0] for entry in output.call_args_list
            if "text_det_limit_type=" in entry.args[0]
        ]
        self.assertEqual(len(config_logs), 1)
        self.assertIn("text_det_limit_type=max", config_logs[0])
        self.assertIn("text_det_limit_side_len=640", config_logs[0])

    def test_predict_receives_effective_detector_limits_for_every_page(self):
        for limit_type, side_len in (("max", 960), ("resize_long", 640)):
            with self.subTest(limit_type=limit_type, side_len=side_len), TemporaryDirectory() as root:
                pdf = Path(root) / "input.pdf"
                pdf.write_bytes(b"mock PDF; rendering is stubbed")
                output_dir = Path(root) / "ocr"
                pages = [output_dir / "page_1.png", output_dir / "page_2.png"]
                results = [Mock(), Mock()]
                model = Mock()
                model.predict.side_effect = [[results[0]], [results[1]]]
                with patch.object(ocr_service, "HOST_MEDIA_ROOT", root), patch.object(
                    ocr_service, "CONTAINER_MEDIA_ROOT", "/app/media"
                ), patch.object(ocr_service, "pdf_to_images", return_value=pages) as renderer, patch.object(
                    ocr_service, "get_ocr", return_value=model
                ), patch.object(ocr_service, "OCR_DET_LIMIT_TYPE", limit_type), patch.object(
                    ocr_service, "OCR_DET_LIMIT_SIDE_LEN", side_len
                ):
                    response = ocr_service.run_ocr("/app/media/input.pdf", "/app/media/ocr", True)
                renderer.assert_called_once_with(pdf, output_dir)
                self.assertEqual(model.predict.call_args_list, [
                    call(str(page), text_det_limit_type=limit_type, text_det_limit_side_len=side_len)
                    for page in pages
                ])
                self.assertEqual(response["json_count"], 2)
                self.assertEqual(response["image_count"], 2)
                for result in results:
                    result.save_to_json.assert_called_once_with(save_path=str(output_dir))
                    result.save_to_img.assert_called_once_with(save_path=str(output_dir))

    def test_cached_ocr_results_still_skip_prediction(self):
        with TemporaryDirectory() as root:
            pdf = Path(root) / "input.pdf"
            pdf.touch()
            output_dir = Path(root) / "ocr"
            output_dir.mkdir()
            (output_dir / "page_1_res.json").write_text("{}")
            with patch.object(ocr_service, "HOST_MEDIA_ROOT", root), patch.object(
                ocr_service, "CONTAINER_MEDIA_ROOT", "/app/media"
            ), patch.object(ocr_service, "pdf_to_images") as renderer, patch.object(
                ocr_service, "get_ocr"
            ) as model:
                response = ocr_service.run_ocr("/app/media/input.pdf", "/app/media/ocr")
            self.assertTrue(response["skipped"])
            renderer.assert_not_called()
            model.assert_not_called()

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

