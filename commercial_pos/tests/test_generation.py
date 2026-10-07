import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.batch_facts_service import read_batch_facts
from commercial_pos.services.generation_service import build_commercial_snapshot, ensure_commercial_po_for_batch
from commercial_pos.services.price_service import CommercialPOError, resolve_commercial_price
from documents.models import DocumentSequence, GeneratedDocument
from backorders.models import InventoryAllocation, InventoryBatch, InventoryItem
from finance.services.finance_analysis_service import build_finance_dashboard_data
from finance.services.settlement_finance_service import build_settlement_finance_dashboard_data
from settlements.models import PaymentTransaction, SettlementAccount
from settlements.services.settlement_auto_service import ensure_settlement_account_for_document
from workflow.services.workflow_document_generation_service import generate_documents_for_workflow_item

from .fixtures import FixtureMixin, fake_legacy_writer, fake_render, make_factory_po


class CommercialPriceTests(FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.factory_po = make_factory_po(self.user, self.batch)

    def test_inclusive_boundary(self):
        for day, price in [(19, "165.00"), (20, "160.00"), (22, "160.00")]:
            with self.subTest(day=day):
                self.assertEqual(resolve_commercial_price(date(2026, 4, day))[1], Decimal(price))

    def test_shipping_date_not_order_date_and_no_discount(self):
        data = build_commercial_snapshot(self.batch)
        self.assertEqual(self.order.order_date, date(2026, 4, 13))
        self.assertEqual(data["unit_price"], "160.00")
        self.assertEqual(data["po_data"]["items"][0]["discount"], "0.00%")
        self.assertEqual(data["po_data"]["items"][0]["unit_price"], "160.00")

    def test_53_units_are_8480(self):
        self.batch_item.shipped_quantity = 53
        self.batch_item.save()
        self.serial.raw_data = {"delivered_quantity": 53}
        self.serial.save()
        payload = self.factory_po.source_data["po_data"]
        payload["items"][0].update(quantity_raw=53.0, quantity="53.00", batch_quantity="53")
        payload["totals"].update(total_units_raw=53.0, total_units="53")
        self.factory_po.save(update_fields=["source_data"])
        data = build_commercial_snapshot(self.batch)
        self.assertEqual(data["total_units"], "53")
        self.assertEqual(data["total_amount"], "8480.00")

    def test_missing_or_conflicting_dates_never_fallback(self):
        for batch_date, confirmation_date in [(None, date(2026, 4, 22)), (date(2026, 4, 22), None), (date(2026, 4, 22), date(2026, 4, 13))]:
            with self.subTest(batch_date=batch_date, confirmation_date=confirmation_date):
                self.batch.batch_date = batch_date
                self.batch.factory_confirmation.shipping_date = confirmation_date
                with self.assertRaises(ValueError):
                    build_commercial_snapshot(self.batch)

    def test_missing_and_overlapping_rules_rejected(self):
        CommercialPOPricePolicy.objects.update(is_active=False)
        with self.assertRaises(CommercialPOError):
            resolve_commercial_price(date(2026, 4, 22))
        CommercialPOPricePolicy.objects.update(is_active=True)
        CommercialPOPricePolicy.objects.create(name="ambiguous newest", unit_price=Decimal("150.00"))
        with self.assertRaises(CommercialPOError):
            resolve_commercial_price(date(2026, 4, 22))

    def test_invalid_price_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommercialPOPricePolicy.objects.create(name="invalid", unit_price=Decimal("0.00"))

    def test_batch_serial_quantity_and_identity_conflicts(self):
        for raw in ({"delivered_quantity": 2}, {"delivered_quantity": "broken"}, {"delivered_quantity": None}):
            self.serial.raw_data = raw
            self.serial.save()
            with self.assertRaises(CommercialPOError):
                read_batch_facts(self.batch)
        self.serial.raw_data = {"delivered_quantity": 1}
        self.serial.product_code = "OTHER"
        self.serial.save()
        with self.assertRaises(CommercialPOError):
            read_batch_facts(self.batch)

    def test_whole_order_quantity_is_not_used(self):
        self.order_item.requested_quantity = 1000
        self.order_item.confirmed_quantity = 999
        self.order_item.save()
        self.assertEqual(build_commercial_snapshot(self.batch)["total_units"], "1")


class CommercialGenerationTests(FixtureMixin, TransactionTestCase):
    def generate_original_pair(self):
        with patch("workflow.services.workflow_document_generation_service.write_invoice_html_and_pdf", side_effect=fake_legacy_writer), patch(
            "workflow.services.workflow_document_generation_service.write_po_html_and_pdf", side_effect=fake_legacy_writer
        ):
            return generate_documents_for_workflow_item(self.workflow, self.user)

    def test_one_operation_three_documents_and_old_status(self):
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            self.generate_original_pair()
        self.assertEqual(set(GeneratedDocument.objects.filter(shipment_batch=self.batch).values_list("document_type", flat=True)),
                         {"hospital_invoice", "factory_po", "commercial_po"})
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.workflow_status, "generated")
        self.assertEqual(self.workflow.po_document.document_type, "factory_po")
        self.assertEqual(self.workflow.invoice_document.document_type, "hospital_invoice")
        self.assertEqual(SettlementAccount.objects.count(), 2)

    def test_extension_failure_preserves_original_success(self):
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=RuntimeError("Synthetic render failure")), self.assertLogs("commercial_pos.services.generation_service", level="ERROR") as logs:
            self.generate_original_pair()
        self.assertIn(f"batch_id={self.batch.pk}", logs.output[0])
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.workflow_status, "generated")
        self.assertEqual((self.workflow.invoice_status, self.workflow.po_status), ("generated", "generated"))
        self.assertEqual(GeneratedDocument.objects.count(), 2)
        self.assertEqual(SettlementAccount.objects.count(), 2)

    def test_original_rollback_never_runs_extension(self):
        with patch("commercial_pos.services.generation_service.render_commercial_files") as render:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self.generate_original_pair()
                    self.assertFalse(GeneratedDocument.objects.filter(document_type="commercial_po").exists())
                    raise RuntimeError("rollback original transaction")
            render.assert_not_called()
        self.assertFalse(GeneratedDocument.objects.exists())

    def test_idempotent_retry_preserves_old_files_numbers_finance(self):
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=RuntimeError("fail")), self.assertLogs("commercial_pos.services.generation_service", level="ERROR"):
            self.generate_original_pair()
        old_documents = list(GeneratedDocument.objects.order_by("id").values())
        old_sequences = list(DocumentSequence.objects.values())
        old_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in Path(self.media.name).rglob("*") if path.is_file()}
        finance = build_finance_dashboard_data()
        settlement_finance = build_settlement_finance_dashboard_data()
        settlement_finance.pop("generated_at", None)
        accounts = list(SettlementAccount.objects.values())
        payments = list(PaymentTransaction.objects.values())
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render) as render:
            first = ensure_commercial_po_for_batch(self.batch.pk, self.user)
            second = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        self.assertEqual(first.pk, second.pk)
        render.assert_called_once()
        self.assertEqual(list(GeneratedDocument.objects.exclude(document_type="commercial_po").order_by("id").values()), old_documents)
        self.assertEqual(list(DocumentSequence.objects.values()), old_sequences)
        for path, digest in old_files.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertEqual(build_finance_dashboard_data(), finance)
        after_settlement_finance = build_settlement_finance_dashboard_data()
        after_settlement_finance.pop("generated_at", None)
        self.assertEqual(after_settlement_finance, settlement_finance)
        self.assertEqual(list(SettlementAccount.objects.values()), accounts)
        self.assertEqual(list(PaymentTransaction.objects.values()), payments)
        self.assertEqual(ensure_settlement_account_for_document(first)["status"], "unsupported")

    def test_render_and_database_failures_leave_no_success_or_old_file_damage(self):
        make_factory_po(self.user, self.batch)
        sentinel = Path(self.media.name) / "sentinel.pdf"
        sentinel.write_bytes(b"original")
        for error in ("render", "database"):
            with self.subTest(error=error):
                if error == "render":
                    patches = [patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=RuntimeError("render"))]
                else:
                    patches = [patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render),
                               patch("commercial_pos.services.generation_service.GeneratedDocument.objects.create", side_effect=RuntimeError("db"))]
                from contextlib import ExitStack
                with ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    with self.assertRaises(RuntimeError):
                        ensure_commercial_po_for_batch(self.batch.pk, self.user)
                self.assertFalse(GeneratedDocument.objects.filter(document_type="commercial_po").exists())
                self.assertEqual(sentinel.read_bytes(), b"original")
                self.assertFalse(list(Path(self.media.name).rglob(".pending-*")))

    def test_database_constraint_targets_commercial_type_only(self):
        make_factory_po(self.user, self.batch)
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            doc = ensure_commercial_po_for_batch(self.batch.pk, self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeneratedDocument.objects.create(order=self.order, shipment_batch=self.batch, document_type="commercial_po", document_number="OTHER-CPO", generated_by=self.user)
        for number in ("OLD-PO-1", "OLD-PO-2"):
            GeneratedDocument.objects.create(order=self.order, shipment_batch=self.batch, document_type="factory_po", document_number=number, generated_by=self.user)
        self.assertEqual(GeneratedDocument.objects.filter(document_number__in=("OLD-PO-1", "OLD-PO-2")).count(), 2)
        self.assertTrue(Path(doc.pdf_file.path).is_file())

    def test_generation_refuses_outer_transaction(self):
        with transaction.atomic(), self.assertRaises(CommercialPOError):
            ensure_commercial_po_for_batch(self.batch.pk, self.user)

    def test_uncertain_commit_never_deletes_files_that_database_may_reference(self):
        make_factory_po(self.user, self.batch)
        commit = connection.commit
        def commit_then_lose_ack():
            commit()
            raise OperationalError("Synthetic lost commit acknowledgement")
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render), patch.object(
            connection, "commit", side_effect=commit_then_lose_ack
        ), self.assertLogs("commercial_pos.services.generation_service", level="WARNING"):
            with self.assertRaises(OperationalError):
                ensure_commercial_po_for_batch(self.batch.pk, self.user)
        document = GeneratedDocument.objects.get(document_type="commercial_po")
        self.assertTrue(Path(document.pdf_file.path).is_file())
        self.assertTrue(Path(document.html_file.path).is_file())

    def test_inventory_keeps_factory_po_skip_and_uses_physical_batch_date(self):
        allocation = InventoryAllocation.objects.create(order=self.order, product=self.order_item.product,
            product_code=self.order_item.product_code, status="shipment_created", allocated_count=1)
        inventory_batch = InventoryBatch.objects.create(factory=self.order.factory)
        InventoryItem.objects.create(batch=inventory_batch, product=self.order_item.product,
            product_code=self.order_item.product_code, serial_number="INVENTORY-SYNTHETIC",
            expiration_date=date(2028, 1, 1), allocated_order=self.order, allocation=allocation, status="allocated")
        self.batch.factory_confirmation = None
        self.batch.inventory_allocation = allocation
        self.batch.source_type = "inventory_allocation"
        self.batch.save()
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render) as render, self.assertLogs("commercial_pos.services.generation_service", level="ERROR"):
            result = self.generate_original_pair()
        render.assert_not_called()
        self.assertTrue(result["factory_po"]["skipped"])
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.workflow_status, "generated")
        self.assertIsNone(self.workflow.po_document_id)
        self.assertFalse(GeneratedDocument.objects.filter(document_type="commercial_po").exists())
        self.assertFalse(GeneratedDocument.objects.filter(document_type="factory_po").exists())

    def test_corrupt_rendered_pdf_is_not_published(self):
        make_factory_po(self.user, self.batch)
        def corrupt_writer(**kwargs):
            kwargs["html_path"].write_text(kwargs["html_content"])
            kwargs["pdf_path"].write_bytes(b"%PDF-1.7\n" + b"invalid data" * 30)
        with patch("commercial_pos.services.generation_service.write_po_html_and_pdf", side_effect=corrupt_writer):
            with self.assertRaises(CommercialPOError):
                ensure_commercial_po_for_batch(self.batch.pk, self.user)
        self.assertFalse(GeneratedDocument.objects.filter(document_type="commercial_po").exists())

    def test_order_admin_wrapper_defers_until_atomic_commit(self):
        from documents.services.document_generation_service import generate_all_documents_for_order
        with patch("workflow.services.workflow_document_generation_service.write_invoice_html_and_pdf", side_effect=fake_legacy_writer), patch(
            "workflow.services.workflow_document_generation_service.write_po_html_and_pdf", side_effect=fake_legacy_writer
        ), patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render) as render:
            with transaction.atomic():
                generate_all_documents_for_order(self.order, self.user)
                render.assert_not_called()
                self.workflow.refresh_from_db()
                self.assertEqual(self.workflow.workflow_status, "generated")
            render.assert_called_once()

    def test_command_default_dry_run_apply_and_retry_only_commercial(self):
        make_factory_po(self.user, self.batch)
        out = StringIO()
        call_command("backfill_commercial_pos", batch_id=[self.batch.pk], stdout=out)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("eligible=1", out.getvalue())
        self.assertFalse(GeneratedDocument.objects.filter(document_type="commercial_po").exists())
        with self.assertRaises(CommandError):
            call_command("backfill_commercial_pos")
        with self.assertRaises(CommandError):
            call_command("backfill_commercial_pos", batch_id=[self.batch.pk], apply=True)
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            call_command("backfill_commercial_pos", bon=[self.order.bon_de_commande], apply=True, actor_id=self.user.pk, stdout=StringIO())
        self.assertEqual(GeneratedDocument.objects.filter(document_type="commercial_po").count(), 1)
        self.assertFalse(DocumentSequence.objects.exists())
        self.assertFalse(SettlementAccount.objects.exists())
        out = StringIO()
        call_command("backfill_commercial_pos", all=True, stdout=out)
        self.assertIn("existing=1", out.getvalue())

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks and partial unique index")
    def test_concurrent_generation_publishes_one_document(self):
        make_factory_po(self.user, self.batch)
        barrier = Barrier(2)
        def renderer(snapshot, directory):
            self.assertFalse(connection.in_atomic_block)
            fake_render(snapshot, directory)
            barrier.wait(timeout=20)
        def generate():
            close_old_connections()
            try:
                return ensure_commercial_po_for_batch(self.batch.pk, self.user).pk
            finally:
                close_old_connections()
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=renderer), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: generate(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(GeneratedDocument.objects.filter(document_type="commercial_po").count(), 1)
        self.assertEqual(len(list((Path(self.media.name) / "commercial_purchase_orders").rglob("document.pdf"))), 1)
