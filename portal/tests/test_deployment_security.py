import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config.upload_validation import validate_pdf_upload


class PDFUploadValidationTests(SimpleTestCase):
    def upload(self, name="document.pdf", content=b"%PDF-1.7\nfixture", content_type="application/pdf"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_valid_pdf_is_accepted_and_stream_position_is_restored(self):
        uploaded = self.upload()
        uploaded.seek(3)
        validate_pdf_upload(uploaded)
        self.assertEqual(uploaded.tell(), 3)

    def test_non_pdf_extension_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf_upload(self.upload(name="document.txt"))

    def test_non_pdf_content_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf_upload(self.upload(content_type="text/plain"))

    def test_non_pdf_signature_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf_upload(self.upload(content=b"not a pdf"))

    @override_settings(MAX_PDF_UPLOAD_SIZE=8)
    def test_oversized_pdf_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf_upload(self.upload(content=b"%PDF-1234"))


class DeploymentAccessSecurityTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)
        self.settings_override = override_settings(
            DEBUG=False,
            MEDIA_ROOT=self.temp_media.name,
            MEDIA_URL="/portal/files/",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.staff = get_user_model().objects.create_user(
            username="deployment-staff",
            password="test-password",
            is_staff=True,
        )
        self.non_staff = get_user_model().objects.create_user(
            username="deployment-non-staff",
            password="test-password",
            is_staff=False,
        )
        self.business_paths = (
            "hospital_orders/test.pdf",
            "factory_confirmations/test.pdf",
            "inventory_batches/test.pdf",
            "generated_documents/pdf/test.pdf",
            "generated_documents/html/test.html",
            "orders/workspace/ocr/page_1.png",
            "orders/workspace/ocr/page_1_res.json",
        )
        for relative_path in self.business_paths:
            target = Path(self.temp_media.name, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-1.7\nsynthetic")

    def protected_url(self, path):
        return reverse("portal:protected_media", kwargs={"path": path})

    def test_anonymous_user_cannot_access_any_business_document(self):
        for path in self.business_paths:
            with self.subTest(path=path):
                response = self.client.get(self.protected_url(path))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response.url)

    def test_authenticated_non_staff_user_cannot_access_business_documents(self):
        self.client.force_login(self.non_staff)
        response = self.client.get(self.protected_url(self.business_paths[0]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_staff_user_can_stream_business_document(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.protected_url(self.business_paths[0]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7\nsynthetic")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_path_traversal_is_rejected(self):
        outside = (
            Path(self.temp_media.name).parent
            / f"{Path(self.temp_media.name).name}-outside.pdf"
        )
        outside.write_bytes(b"%PDF-1.7\noutside")
        self.addCleanup(outside.unlink, missing_ok=True)
        self.client.force_login(self.staff)
        response = self.client.get("/portal/files/%2E%2E/outside.pdf")
        self.assertEqual(response.status_code, 404)

    def test_generic_media_url_is_not_routed_when_debug_is_false(self):
        self.client.force_login(self.staff)
        response = self.client.get("/media/hospital_orders/test.pdf")
        self.assertEqual(response.status_code, 404)

    def test_finance_rejects_anonymous_and_non_staff_users(self):
        for url in ("/portal/finance/", "/portal/finance/export.xlsx"):
            with self.subTest(url=url, user="anonymous"):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)

            self.client.force_login(self.non_staff)
            with self.subTest(url=url, user="non-staff"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response.url)
            self.client.logout()
