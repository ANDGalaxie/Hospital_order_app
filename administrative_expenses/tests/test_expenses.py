import json
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from administrative_expenses.categories import CATEGORIES, SUBCATEGORIES
from administrative_expenses.forms import ExpenseForm
from administrative_expenses.models import AdministrativeExpense as Expense, AdministrativeExpenseAttachment as Attachment
from administrative_expenses.services import UploadJournal, monthly_summary, recover_files, save_expense
from administrative_expenses.storage import private_storage
from administrative_expenses.validation import MAX_BATCH_SIZE, MAX_FILE_SIZE, validate_upload, validate_uploads
from portal.role_access import INTERNAL_GROUP

PREFIX = "portal:administrative_expenses:"


def url(name, *args):
    return reverse(PREFIX + name, args=args)


def pdf(name="receipt.pdf"):
    with fitz.open() as document:
        document.new_page()
        content = document.tobytes()
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def image(name="receipt.png", fmt="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (3, 3), "white").save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def consume_response(response):
    # Django's test client wraps stream completion to preserve the test transaction.
    if response.streaming:
        list(response.streaming_content)


def data(**changes):
    values = {"category": "salary", "subcategory": "", "expense_date": "2026-09-01",
              "employee_or_payee": "Employee", "amount": "123.45", "description": "Synthetic expense",
              "payment_status": "pending", "paid_at": "", "document_type": "other"}
    values.update(changes)
    return values


class UploadValidationTests(SimpleTestCase):
    def test_pdf_and_images_with_matching_content(self):
        for upload in (pdf(), image(), image("receipt.jpg", "JPEG"), image("receipt.jpeg", "JPEG")):
            upload.seek(2)
            self.assertIs(validate_upload(upload), upload)
            self.assertEqual(upload.tell(), 2)

    def test_reject_unsupported_and_forged_formats(self):
        uploads = [pdf("receipt.html"), pdf("receipt.svg"), pdf("receipt.exe"),
                   SimpleUploadedFile("receipt.pdf", b"%PDF-fake"),
                   SimpleUploadedFile("receipt.png", b"<html>bad</html>"),
                   image("receipt.jpg"), image("receipt.pdf"),
                   SimpleUploadedFile("receipt.png", image().read()[:20])]
        for upload in uploads:
            with self.subTest(name=upload.name), self.assertRaises(ValidationError):
                validate_upload(upload)

    def test_reject_unsafe_original_names(self):
        for name in ("../receipt.pdf", "folder/receipt.pdf", "..\\receipt.pdf", "receipt\n.pdf", ".hidden.pdf"):
            upload = pdf()
            upload.name = "receipt.pdf"
            # UploadedFile already sanitizes slash basenames; test the validation boundary directly.
            upload._name = name
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_upload(upload)

    def test_reject_empty_single_and_batch_size_limits(self):
        upload = pdf()
        upload.size = MAX_FILE_SIZE + 1
        with self.assertRaises(ValidationError):
            validate_upload(upload)
        with self.assertRaises(ValidationError):
            validate_upload(SimpleUploadedFile("empty.pdf", b""))
        uploads = [pdf() for _ in range(3)]
        for upload in uploads:
            upload.size = MAX_FILE_SIZE
        with self.assertRaises(ValidationError):
            validate_uploads(uploads)


class ExpenseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boss = get_user_model().objects.create_user(username="Acoeurs", is_staff=True)
        cls.staff = get_user_model().objects.create_user(username="ordinary-staff", is_staff=True)
        cls.claire = get_user_model().objects.create_user(username="Claire", is_staff=True)
        cls.cynthia = get_user_model().objects.create_user(username="Cynthia", is_staff=True)
        cls.other_admin = get_user_model().objects.create_user(username="other-admin", is_staff=True, is_superuser=True)
        cls.similar = get_user_model().objects.create_user(username="acoeurs", is_staff=True, is_superuser=True)

    def setUp(self):
        self.media = tempfile.TemporaryDirectory(prefix="administrative-expenses-test-")
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name, ROOT_URLCONF="administrative_expenses.tests.debug_urls")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client.force_login(self.boss)

    def create(self, **changes):
        response = self.client.post(url("create"), data(**changes))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return Expense.objects.latest("pk")

    def attach(self, expense, uploads=None, kind="other"):
        response = self.client.post(url("upload", expense.pk), {"document_type": kind, "files": uploads or [pdf()]})
        self.assertEqual(response.status_code, 302)
        return expense.attachments.filter(removed_at__isnull=True).latest("pk")

    def test_create_without_evidence_and_actor_fields(self):
        expense = self.create()
        self.assertEqual(expense.amount, Decimal("123.45"))
        self.assertEqual(expense.created_by, self.boss)
        self.assertEqual(expense.updated_by, self.boss)
        self.assertFalse(expense.has_supporting_document)
        self.assertContains(self.client.get(url("detail", expense.pk)), "待补凭证")

    def test_edit_preserves_existing_attachments(self):
        expense = self.create()
        attachment = self.attach(expense)
        response = self.client.post(url("edit", expense.pk), data(amount="234.56", description="Updated"))
        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("234.56"))
        self.assertEqual(expense.description, "Updated")
        self.assertTrue(private_storage.exists(attachment.file.name))
        self.assertEqual(expense.attachments.count(), 1)

    def test_create_and_append_multiple_document_types(self):
        expense = self.create(files=[pdf(), image()], document_type="payslip")
        self.attach(expense, [pdf("bank.pdf")], "payment_proof")
        self.assertEqual(expense.attachments.count(), 3)
        self.assertEqual(set(expense.attachments.values_list("document_type", flat=True)), {"payslip", "payment_proof"})
        self.assertContains(self.client.get(url("detail", expense.pk)), "已上传凭证")
        self.assertContains(self.client.get(url("edit", expense.pk)), "receipt.pdf")

    def test_removing_last_file_restores_missing_status(self):
        expense = self.create()
        attachment = self.attach(expense)
        self.assertTrue(expense.has_supporting_document)
        response = self.client.post(url("remove", expense.pk, attachment.pk))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(expense.has_supporting_document)
        self.assertFalse(private_storage.exists(attachment.file.name))
        self.assertContains(self.client.get(url("detail", expense.pk)), "待补凭证")

    def test_remove_one_of_multiple_keeps_attached_status(self):
        expense = self.create(files=[pdf(), image()])
        attachment = expense.attachments.first()
        self.client.post(url("remove", expense.pk, attachment.pk))
        self.assertTrue(expense.has_supporting_document)
        self.assertEqual(expense.attachments.count(), 1)

    def test_download_and_explicit_preview_have_safe_headers(self):
        expense = self.create()
        attachment = self.attach(expense, [pdf("工资单.pdf")])
        for name, disposition in (("download", "attachment"), ("preview", "inline")):
            response = self.client.get(url(name, expense.pk, attachment.pk))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertTrue(response["Content-Disposition"].startswith(disposition))
            self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
            consume_response(response)
        self.assertRegex(attachment.file.name, rf"administrative_expenses/{expense.pk}/[0-9a-f]{{32}}\.pdf")
        self.assertEqual(os.stat(attachment.file.path).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            _ = attachment.file.url

    def test_cross_expense_ids_cannot_download_preview_or_remove(self):
        expense = self.create(files=[pdf()])
        other = self.create()
        attachment = expense.attachments.first()
        for name in ("download", "preview"):
            self.assertEqual(self.client.get(url(name, other.pk, attachment.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("remove", other.pk, attachment.pk)).status_code, 404)
        self.assertTrue(private_storage.exists(attachment.file.name))
        Attachment.objects.filter(pk=attachment.pk).update(expense=other)
        self.assertEqual(self.client.get(url("download", other.pk, attachment.pk)).status_code, 404)

    def test_all_nonboss_users_denied_every_endpoint(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        gets = [url("home"), url("category", "salary"), url("category", "benefits"),
                url("list", "benefits", "bonus"), url("create"), url("edit", expense.pk),
                url("detail", expense.pk), url("download", expense.pk, attachment.pk),
                url("preview", expense.pk, attachment.pk)]
        posts = [url("create"), url("edit", expense.pk), url("void", expense.pk),
                 url("upload", expense.pk), url("remove", expense.pk, attachment.pk)]
        for user in (None, self.staff, self.claire, self.cynthia, self.other_admin, self.similar):
            client = Client()
            if user:
                client.force_login(user)
            for path in gets:
                with self.subTest(user=user, path=path):
                    self.assertEqual(client.get(path).status_code, 403)
            for path in posts:
                with self.subTest(user=user, path=path):
                    self.assertEqual(client.post(path, data()).status_code, 403)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_inactive_boss_denied(self):
        self.boss.is_active = False
        self.boss.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(url("home")).status_code, 403)

    def test_generic_media_and_outputs_deny_private_files_and_aliases_for_everyone(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        alias = Path(self.media.name, "public-alias.pdf")
        alias.symlink_to(Path(attachment.file.path))
        folder = Path(self.media.name, "alias-folder")
        folder.symlink_to(Path(self.media.name, "administrative_expenses"))
        names = [attachment.file.name, "public-alias.pdf",
                 "alias-folder/" + attachment.file.name.split("/", 1)[1],
                 "./administrative_expenses/../" + attachment.file.name,
                 attachment.file.name.replace("/", "/./", 1)]
        for user in (self.boss, self.staff, self.claire, self.cynthia, None):
            client = Client()
            if user:
                client.force_login(user)
            for name in names:
                for prefix in ("/portal/files/", "/media/", "/outputs/"):
                    with self.subTest(user=user, prefix=prefix, name=name):
                        response = client.get(prefix + name)
                        self.assertIn(response.status_code, (302, 403, 404))
                        consume_response(response)

    def test_private_storage_symlink_escape_is_rejected_for_read_and_upload(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        target = Path(attachment.file.path)
        target.unlink()
        with tempfile.TemporaryDirectory(prefix="outside-private-") as outside:
            external = Path(outside, "outside.pdf")
            external.write_bytes(pdf().read())
            target.symlink_to(external)
            self.assertEqual(self.client.get(url("download", expense.pk, attachment.pk)).status_code, 404)
            target.unlink()
            parent = target.parent
            parent.rmdir()
            parent.symlink_to(outside, target_is_directory=True)
            response = self.client.post(url("upload", expense.pk), {"files": [pdf()], "document_type": "invoice"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(len(list(Path(outside).iterdir())), 1)
            self.assertTrue(external.exists())
            parent.unlink()

    def test_storage_rejects_traversal_and_foreign_prefix(self):
        for name in ("../secret.pdf", "hospital_orders/x.pdf", "administrative_expenses/1/../../secret.pdf"):
            with self.subTest(name=name), self.assertRaises(SuspiciousFileOperation):
                private_storage.path(name)

    def test_final_response_boundary_blocks_old_file_endpoint_alias(self):
        from django.http import FileResponse
        from django.test import RequestFactory
        from administrative_expenses.middleware import PrivateExpenseResponseMiddleware
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        request = RequestFactory().get("/legacy/download/")
        request.user = self.boss
        response = FileResponse(private_storage.open(attachment.file.name))
        self.assertEqual(PrivateExpenseResponseMiddleware(lambda req: response)(request).status_code, 403)

    def test_get_has_no_business_writes_and_actions_require_post(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        paths = [url("home"), url("category", "salary"), url("category", "benefits"), url("list", "benefits", "bonus"),
                 url("create"), url("edit", expense.pk), url("detail", expense.pk), url("download", expense.pk, attachment.pk)]
        for path in paths:
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(path)
                consume_response(response)
            writes = [query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
            self.assertEqual(writes, [], path)
        for path in (url("void", expense.pk), url("upload", expense.pk), url("remove", expense.pk, attachment.pk)):
            self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_csrf_protects_all_business_mutations(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.boss)
        for path in (url("create"), url("edit", expense.pk), url("void", expense.pk),
                     url("upload", expense.pk), url("remove", expense.pk, attachment.pk)):
            self.assertEqual(client.post(path, data()).status_code, 403)
        response = client.get(url("detail", expense.pk))
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post(url("remove", expense.pk, attachment.pk), {"csrfmiddlewaretoken": token}).status_code, 302)

    def test_payment_and_document_status_are_independent(self):
        for paid in (False, True):
            for attached in (False, True):
                expense = self.create(payment_status="paid" if paid else "pending",
                                      paid_at="2026-09-03" if paid else "", files=[pdf()] if attached else [])
                self.assertEqual(expense.payment_status, "paid" if paid else "pending")
                self.assertEqual(expense.has_supporting_document, attached)
        expense = Expense.objects.filter(payment_status="pending").first()
        self.client.post(url("edit", expense.pk), data(payment_status="paid", paid_at="2026-09-03"))
        expense.refresh_from_db()
        self.assertEqual(expense.payment_status, "paid")

    def test_all_valid_categories_use_shared_form_and_list(self):
        for category in CATEGORIES:
            for subcategory in SUBCATEGORIES.get(category, {"": ""}):
                expense = self.create(category=category, subcategory=subcategory)
                self.assertEqual(expense.category, category)
                path = url("list", category, subcategory) if subcategory else url("category", category)
                response = self.client.get(path + "?month=2026-09")
                self.assertContains(response, "123.45")
                self.assertContains(response, "€")

    def test_bad_category_routes_return_404_and_bad_month_returns_400(self):
        for path in (url("category", "bad"), url("list", "salary", "bonus"), url("list", "benefits", "travel")):
            self.assertEqual(self.client.get(path).status_code, 404)
        for month in ("2026-13", "2026-9", "bad", "0000-01", "9999-12"):
            for path in (url("home"), url("category", "salary"), url("category", "benefits")):
                self.assertEqual(self.client.get(path, {"month": month}).status_code, 400)

    def test_category_payee_amount_and_payment_validation(self):
        invalid = [
            {"category": "invalid"}, {"category": "salary", "subcategory": "bonus"},
            {"category": "benefits", "subcategory": ""}, {"category": "benefits", "subcategory": "travel"},
            {"category": "reimbursement", "subcategory": "bonus"}, {"employee_or_payee": ""},
            {"employee_or_payee": "   "}, {"amount": "-1"}, {"amount": "NaN"}, {"amount": "Infinity"},
            {"amount": "0.001"}, {"amount": "abc"}, {"amount": "10000000000.00"},
            {"payment_status": "paid", "paid_at": ""}, {"paid_at": "2026-09-04"},
            {"payment_status": "paid", "paid_at": "2026-08-31"},
            {"payment_status": "paid", "paid_at": (timezone.localdate() + timedelta(days=1)).isoformat()},
            {"payment_status": "unknown"}, {"expense_date": "invalid"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.client.post(url("create"), data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Expense.objects.exists())
        self.create(category="office_supplies", employee_or_payee="", amount="0.01")

    def test_database_constraints_reject_invalid_state(self):
        expense = self.create()
        for changes in ({"amount": "-1"}, {"category": "bad"}, {"subcategory": "bonus"},
                        {"payment_status": "paid"}, {"payment_status": "invalid"},
                        {"payment_status": "paid", "paid_at": date(2026, 8, 1)}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    Expense.objects.filter(pk=expense.pk).update(**changes)

    def test_void_retains_audit_and_excludes_statistics(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        self.assertEqual(self.client.post(url("void", expense.pk)).status_code, 302)
        expense.refresh_from_db()
        self.assertTrue(expense.is_void)
        self.assertEqual(monthly_summary("2026-09")["summary"]["total"], Decimal("0.00"))
        self.assertEqual(self.client.get(url("edit", expense.pk)).status_code, 302)
        self.assertEqual(self.client.post(url("upload", expense.pk), {"files": [pdf()], "document_type": "other"}).status_code, 404)
        response = self.client.get(url("download", expense.pk, attachment.pk))
        self.assertEqual(response.status_code, 200)
        consume_response(response)
        self.assertContains(self.client.get(url("category", "salary"), {"month": "2026-09", "void": "1"}), "已作废")

    def test_monthly_decimal_totals_are_not_multiplied_by_attachments(self):
        self.create(amount="0.10", files=[pdf(), image()])
        self.create(amount="0.20", category="benefits", subcategory="bonus", payment_status="paid", paid_at="2026-10-01")
        self.create(amount="5.00", category="benefits", subcategory="meals")
        self.create(amount="7.00", category="reimbursement", subcategory="travel")
        voided = self.create(amount="999.00")
        self.client.post(url("void", voided.pk))
        self.create(amount="888.00", expense_date="2026-10-01")
        context = monthly_summary("2026-09")
        self.assertEqual(context["summary"], {"total": Decimal("12.30"), "paid": Decimal("0.20"),
                                             "pending": Decimal("12.10"), "count": 4, "missing": 3})
        rows = {row["code"]: row for row in context["categories"]}
        self.assertEqual(rows["salary"]["total"], Decimal("0.10"))
        self.assertEqual(rows["benefits"]["total"], Decimal("5.20"))
        children = {row["code"]: row for row in rows["benefits"]["children"]}
        self.assertEqual(children["bonus"]["total"], Decimal("0.20"))
        self.assertEqual(children["meals"]["total"], Decimal("5.00"))
        self.assertEqual(rows["reimbursement"]["total"], Decimal("7.00"))
        response = self.client.get(url("category", "salary"), {"month": "2026-09", "document": "attached"})
        self.assertEqual(response.context["filtered_summary"]["total"], Decimal("0.10"))
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_summary_december_and_empty_month(self):
        context = monthly_summary("2025-12")
        self.assertEqual(context["month"], "2025-12")
        self.assertEqual(context["summary"]["total"], Decimal("0.00"))
        self.assertEqual(context["summary"]["count"], 0)
        self.assertEqual(len(context["categories"]), 5)

    def test_invalid_upload_is_atomic_with_expense_edits(self):
        expense = self.create(files=[pdf()])
        response = self.client.post(url("edit", expense.pk), data(amount="999", files=[SimpleUploadedFile("bad.pdf", b"not pdf")]))
        self.assertEqual(response.status_code, 200)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("123.45"))
        self.assertEqual(expense.attachments.count(), 1)

    def test_failed_second_storage_write_rolls_back_and_cleans_new_files(self):
        original = private_storage._save
        calls = 0
        def failing(name, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Synthetic storage failure")
            return original(name, content)
        with patch.object(private_storage, "_save", side_effect=failing):
            response = self.client.post(url("create"), data(files=[pdf(), image()]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(list(Path(self.media.name).rglob("*.pdf")), [])
        self.assertEqual(list(Path(self.media.name).rglob("*.json")), [])

    def test_database_failure_after_file_write_rolls_back_and_cleans_storage(self):
        with patch.object(Attachment, "save", side_effect=DatabaseError("Synthetic database failure")):
            response = self.client.post(url("create"), data(files=[pdf()]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.exists())
        self.assertEqual(list(Path(self.media.name).rglob("*.pdf")), [])

    def test_failed_cleanup_keeps_managed_journal_then_recovery_removes_orphan(self):
        with patch.object(Attachment, "save", side_effect=DatabaseError("Synthetic failure")), patch.object(private_storage, "delete", side_effect=OSError("Synthetic cleanup failure")):
            response = self.client.post(url("create"), data(files=[pdf()]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.exists())
        self.assertEqual(len(list(Path(self.media.name).rglob("*.json"))), 1)
        self.assertEqual(len(list(Path(self.media.name).rglob("*.pdf"))), 1)
        self.assertEqual(recover_files(), 1)
        self.assertEqual(list(Path(self.media.name).rglob("*.pdf")), [])
        self.assertEqual(list(Path(self.media.name).rglob("*.json")), [])

    def test_recovery_preserves_committed_files_and_skips_live_uploads(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        journal = UploadJournal([attachment.file.name])
        self.assertEqual(recover_files(), 0)
        self.assertEqual(len(list(Path(self.media.name).rglob("*.json"))), 1)
        journal.close()
        self.assertEqual(recover_files(), 0)
        self.assertTrue(private_storage.exists(attachment.file.name))
        self.assertEqual(list(Path(self.media.name).rglob("*.json")), [])

    def test_failed_delete_is_tombstoned_inaccessible_and_retryable(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        with patch.object(private_storage, "delete", side_effect=OSError("Synthetic cleanup failure")):
            self.assertEqual(self.client.post(url("remove", expense.pk, attachment.pk)).status_code, 302)
        attachment.refresh_from_db()
        self.assertIsNotNone(attachment.removed_at)
        self.assertFalse(expense.has_supporting_document)
        self.assertTrue(private_storage.exists(attachment.file.name))
        self.assertEqual(self.client.get(url("download", expense.pk, attachment.pk)).status_code, 404)
        self.assertEqual(recover_files(), 1)
        self.assertFalse(private_storage.exists(attachment.file.name))
        self.assertFalse(Attachment.objects.exists())

    def test_public_admin_models_are_not_registered_or_given_default_permissions(self):
        self.assertNotIn(Expense, admin.site._registry)
        self.assertNotIn(Attachment, admin.site._registry)
        self.assertEqual(Expense._meta.default_permissions, ())
        self.assertEqual(Attachment._meta.default_permissions, ())

    def test_home_card_is_exact_boss_only_and_trilingual(self):
        for user in (self.staff, self.claire, self.cynthia, self.other_admin, self.similar):
            self.client.force_login(user)
            self.assertNotContains(self.client.get("/portal/"), url("home"))
        self.client.force_login(self.boss)
        for code, label in (("zh-hans", "公司行政支出"), ("en", "Administrative Expenses"), ("fr", "Dépenses administratives")):
            self.client.cookies["django_language"] = code
            self.assertContains(self.client.get("/portal/"), label)
            self.assertContains(self.client.get(url("home")), label)
            self.assertContains(self.client.get(url("create")), {"zh-hans": "费用信息", "en": "Expense information", "fr": "Informations sur la dépense"}[code])
        self.boss.groups.add(Group.objects.get_or_create(name=INTERNAL_GROUP)[0])
        self.assertContains(self.client.get("/portal/"), url("home"))

    def test_storage_filename_collision_never_removes_existing_attachment(self):
        expense = self.create(files=[pdf()])
        attachment = expense.attachments.first()
        with patch("administrative_expenses.services.attachment_path", return_value=attachment.file.name):
            response = self.client.post(url("edit", expense.pk), data(amount="999", files=[pdf()]))
        self.assertEqual(response.status_code, 200)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("123.45"))
        self.assertTrue(private_storage.exists(attachment.file.name))
        self.assertEqual(expense.attachments.count(), 1)

    def test_form_rejects_zero_amount(self):
        for value in ("0", "0.00", "-0.00"):
            with self.subTest(amount=value):
                form = ExpenseForm(data(amount=value))
                self.assertFalse(form.is_valid())
                self.assertIn("amount", form.errors)
        self.assertEqual(ExpenseForm().fields["amount"].widget.attrs["min"], "0.01")

    def test_model_rejects_zero_amount(self):
        expense = self.create()
        for value in ("0", "0.00", "-0.00"):
            expense.amount = Decimal(value)
            with self.subTest(amount=value), self.assertRaises(ValidationError) as error:
                expense.full_clean()
            self.assertIn("amount", error.exception.message_dict)

    def test_database_rejects_zero_on_insert_and_update(self):
        expense = self.create()
        with self.assertRaisesMessage(IntegrityError, "admin_expense_positive"):
            with transaction.atomic():
                Expense.objects.create(
                    category="salary", subcategory="", expense_date=date(2026, 9, 1),
                    employee_or_payee="Employee", amount=Decimal("0.00"),
                    created_by=self.boss, updated_by=self.boss,
                )
        with self.assertRaisesMessage(IntegrityError, "admin_expense_positive"):
            with transaction.atomic():
                Expense.objects.filter(pk=expense.pk).update(amount=Decimal("0.00"))
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("123.45"))

    def test_create_and_edit_reject_zero_without_saving(self):
        response = self.client.post(url("create"), data(amount="0.00"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("amount", response.context["form"].errors)
        self.assertFalse(Expense.objects.exists())
        expense = self.create()
        response = self.client.post(url("edit", expense.pk), data(amount="0.00"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("amount", response.context["form"].errors)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("123.45"))

    def test_one_cent_is_valid_in_form_model_and_database(self):
        form = ExpenseForm(data(amount="0.01"))
        self.assertTrue(form.is_valid(), form.errors)
        expense = self.create(amount="0.01")
        expense.full_clean()
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("0.01"))
