from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import CommandError, call_command

from documents.models import DocumentSequence, GeneratedDocument
from orders.models import OrderItem
from workflow.models import DocumentWorkflowItem
from workflow.services.historical_invoice_numbering_apply_service import (
    apply_historical_invoice_numbering,
)
from workflow.tests.test_historical_invoice_numbering_prerender import (
    HistoricalInvoiceNumberingPrerenderTests,
)


class HistoricalInvoiceNumberingApplyTests(
    HistoricalInvoiceNumberingPrerenderTests
):
    def test_26_apply_requires_explicit_confirmation(self):
        with self.assertRaisesMessage(
            CommandError,
            "Historical Invoice apply requires --confirm-historical-invoice-renumbering.",
        ):
            call_command(
                "rebuild_historical_invoice_numbering",
                apply=True,
            )

    def test_27_apply_updates_sequences_documents_and_files(self):
        before_factory_po = GeneratedDocument.objects.get(id=self.factory_po.id)
        before_item = OrderItem.objects.get(order=self.order60)
        original_invoice55_id = self.invoice55.id
        original_invoice60_id = self.invoice60.id
        original_workflow55_id = self.invoice55.shipment_batch.document_workflow_item.invoice_document_id
        original_workflow60_id = self.invoice60.shipment_batch.document_workflow_item.invoice_document_id
        original_pricing_basis = self.invoice60.source_data["pricing_basis"]

        with patch(
            "workflow.services.historical_invoice_numbering_apply_service.write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = apply_historical_invoice_numbering()

        self.assertEqual(result["document_sequence_update_count"], 2)
        self.assertEqual(result["generated_document_update_count"], 4)
        self.assertEqual(result["regenerated_invoice_count"], 4)

        self.sequence55.refresh_from_db()
        self.sequence60.refresh_from_db()
        self.invoice55.refresh_from_db()
        self.invoice55_b2.refresh_from_db()
        self.invoice55_b3.refresh_from_db()
        self.invoice60.refresh_from_db()

        self.assertEqual(self.sequence55.sequence, 1)
        self.assertEqual(self.sequence55.invoice_number, "Invoice 20260105")
        self.assertEqual(self.sequence60.sequence, 5)
        self.assertEqual(self.sequence60.invoice_number, "Invoice 20260505")

        self.assertEqual(self.invoice55.id, original_invoice55_id)
        self.assertEqual(self.invoice60.id, original_invoice60_id)
        self.assertEqual(self.invoice55.document_number, "Invoice 20260105")
        self.assertEqual(self.invoice55_b2.document_number, "Invoice 20260105-B2")
        self.assertEqual(self.invoice55_b3.document_number, "Invoice 20260105-B3")
        self.assertEqual(self.invoice60.document_number, "Invoice 20260505")

        self.assertTrue(str(self.invoice55.pdf_file).endswith("Invoice_20260105.pdf"))
        self.assertTrue(str(self.invoice60.pdf_file).endswith("Invoice_20260505.pdf"))
        self.assertTrue(Path(self.invoice55.pdf_file.path).is_file())
        self.assertTrue(Path(self.invoice60.html_file.path).is_file())
        self.assertIn(
            "Invoice 20260105",
            Path(self.invoice55.html_file.path).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Invoice 20260505",
            Path(self.invoice60.html_file.path).read_text(encoding="utf-8"),
        )

        self.assertEqual(
            self.invoice55.shipment_batch.document_workflow_item.invoice_document_id,
            original_workflow55_id,
        )
        self.assertEqual(
            self.invoice60.shipment_batch.document_workflow_item.invoice_document_id,
            original_workflow60_id,
        )
        self.assertEqual(
            self.invoice60.source_data["pricing_basis"],
            original_pricing_basis,
        )
        self.assertEqual(
            self.invoice60.source_data["numbers"]["invoice_number"],
            "Invoice 20260505",
        )
        self.assertEqual(
            self.invoice60.source_data["numbers"]["base_invoice_number"],
            "Invoice 20260505",
        )
        self.assertEqual(
            self.invoice60.source_data["numbers"]["sequence"],
            5,
        )
        self.assertEqual(
            self.invoice60.source_data["invoice_data"]["invoice"]["invoice_number"],
            "Invoice 20260505",
        )
        self.assertEqual(before_item.hospital_unit_price, 270)
        self.assertEqual(
            OrderItem.objects.get(order=self.order60).hospital_unit_price,
            270,
        )
        self.assertEqual(before_factory_po.document_number, self.factory_po.document_number)
        self.factory_po.refresh_from_db()
        self.assertEqual(self.factory_po.document_number, before_factory_po.document_number)
        self.assertFalse(
            DocumentSequence.objects.filter(
                invoice_number__startswith="TEMP-INVOICE-ORDER-"
            ).exists()
        )
        self.assertFalse(
            GeneratedDocument.objects.filter(
                document_number__startswith="TEMP-INVOICE-DOC-"
            ).exists()
        )

    def test_28_apply_failure_rolls_back_database_and_restores_files(self):
        before_documents = self._database_state()["documents"]
        before_sequences = self._database_state()["sequences"]
        before_files = self._production_fingerprints()

        def fail_after_first(*args, **kwargs):
            if not hasattr(fail_after_first, "count"):
                fail_after_first.count = 0
            fail_after_first.count += 1
            if fail_after_first.count == 2:
                raise RuntimeError("forced render failure")
            return self._write_test_pdf(*args, **kwargs)

        with self.assertRaisesMessage(RuntimeError, "forced render failure"), patch(
            "workflow.services.historical_invoice_numbering_apply_service.write_invoice_html_and_pdf",
            side_effect=fail_after_first,
        ):
            apply_historical_invoice_numbering()

        self.assertEqual(before_sequences, self._database_state()["sequences"])
        self.assertEqual(before_documents, self._database_state()["documents"])
        self.assertEqual(before_files, self._production_fingerprints())

    def test_29_command_apply_runs_with_backup_and_post_apply_summary(self):
        backup_root = Path(self.media_directory.name) / "command-apply-backup"

        def fake_backup_database(backup_root):
            path = Path(backup_root) / "database" / "historical_invoice_numbering.dump"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"backup")
            return path

        output = StringIO()
        with patch(
            "workflow.management.commands.rebuild_historical_invoice_numbering."
            "timestamped_backup_root",
            return_value=backup_root,
        ), patch(
            "workflow.services.historical_invoice_numbering_apply_service._backup_database",
            side_effect=fake_backup_database,
        ), patch(
            "workflow.services.historical_invoice_numbering_apply_service.write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            call_command(
                "rebuild_historical_invoice_numbering",
                apply=True,
                confirm_historical_invoice_renumbering=True,
                stdout=output,
            )
        text = output.getvalue()
        self.assertIn("BACKUP root=", text)
        self.assertIn("APPLY document_sequences=2 generated_documents=4 regenerated_invoices=4", text)
        self.assertIn("POST-APPLY orders=5 correct=2 incorrect=3", text)
        self.assertIn("affected_hospital_invoices=0", text)

    def test_30_apply_preserves_b2_b3_and_generated_document_ids(self):
        ids_before = {
            self.invoice55.id: self.invoice55.document_number,
            self.invoice55_b2.id: self.invoice55_b2.document_number,
            self.invoice55_b3.id: self.invoice55_b3.document_number,
        }
        with patch(
            "workflow.services.historical_invoice_numbering_apply_service.write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            apply_historical_invoice_numbering()
        self.invoice55.refresh_from_db()
        self.invoice55_b2.refresh_from_db()
        self.invoice55_b3.refresh_from_db()
        self.assertEqual(self.invoice55.id, next(iter(ids_before.keys())))
        self.assertEqual(self.invoice55.document_number, "Invoice 20260105")
        self.assertEqual(self.invoice55_b2.document_number, "Invoice 20260105-B2")
        self.assertEqual(self.invoice55_b3.document_number, "Invoice 20260105-B3")
