from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from documents.models import DocumentSequence, GeneratedDocument
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from workflow.services.historical_invoice_numbering_prerender_service import (
    build_historical_invoice_numbering_plan,
    build_batch_invoice_number,
    file_fingerprint,
    prerender_historical_invoice,
)
from workflow.services.workflow_document_generation_service import (
    build_batch_hospital_invoice_data,
)


class HistoricalInvoiceNumberingPrerenderTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media_directory = TemporaryDirectory()
        cls.override = override_settings(
            MEDIA_ROOT=cls.media_directory.name
        )
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        cls.media_directory.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="historical-numbering",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="HISTORICAL FACTORY",
            short_name="HIST",
        )
        department = ProductCategory.objects.create(
            name="HISTORICAL DEPARTMENT",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        factory_node = ProductCategory.objects.create(
            name="HISTORICAL FACTORY NODE",
            parent=department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        category = ProductCategory.objects.create(
            name="HISTORICAL CATEGORY",
            parent=factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product = Product.objects.create(
            code="HIST-001",
            description="Historical product",
            category=category,
            factory=self.factory,
            hospital_unit_price=Decimal("999.00"),
            factory_unit_price=Decimal("999.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Historical Invoice price",
            factory=self.factory,
            category=category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("270.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.order55 = self._create_order(
            55,
            "148859",
            date(2026, 5, 4),
        )
        for pk, bon, day in (
            (56, "149000", 8),
            (57, "149100", 12),
            (58, "149200", 20),
        ):
            self._create_order(pk, bon, date(2026, 5, day))
        self.order60 = self._create_order(
            60,
            "150222",
            date(2026, 5, 29),
        )
        self.sequence55 = DocumentSequence.objects.create(
            month_key="2026-05",
            bon_de_commande=self.order55.bon_de_commande,
            sequence=5,
            invoice_number="Invoice 20260505",
            po_number="",
        )
        self.sequence60 = DocumentSequence.objects.create(
            month_key="2026-05",
            bon_de_commande=self.order60.bon_de_commande,
            sequence=1,
            invoice_number="Invoice 20260105",
            po_number="",
        )
        self.invoice55 = self._create_invoice(
            self.order55,
            batch_number=1,
            current_number="Invoice 20260505",
        )
        self.invoice55_b2 = self._create_invoice(
            self.order55,
            batch_number=2,
            current_number="Invoice 20260505-B2",
        )
        self.invoice55_b3 = self._create_invoice(
            self.order55,
            batch_number=3,
            current_number="Invoice 20260505-B3",
        )
        self.invoice60 = self._create_invoice(
            self.order60,
            batch_number=1,
            current_number="Invoice 20260105",
        )
        self.factory_po = GeneratedDocument.objects.create(
            order=self.order55,
            shipment_batch=self.invoice55.shipment_batch,
            document_type=GeneratedDocument.DocumentType.FACTORY_PO,
            document_number="DELAHK0505S",
            source_data={"po_data": {"items": [{"unit_price": "120"}]}},
            generated_by=self.user,
        )

    def _create_order(self, pk, bon, order_date):
        order = Order.objects.create(
            id=pk,
            bon_de_commande=bon,
            order_date=order_date,
            hospital_name="HISTORICAL HOSPITAL",
            hospital_order_pdf=f"orders/{bon}.pdf",
            factory=self.factory,
            created_by=self.user,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=1,
            confirmed_quantity=1,
            backordered_quantity=0,
            hospital_unit_price=Decimal("270.00"),
            price_policy=self.policy,
            price_policy_date=order_date,
        )
        return order

    def _create_invoice(self, order, batch_number, current_number):
        confirmation = FactoryConfirmation.objects.create(
            order=order,
            factory=self.factory,
            confirmation_type=(
                FactoryConfirmation.ConfirmationType.REPLENISHMENT
            ),
            confirmation_pdf=f"confirmations/{order.id}-{batch_number}.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            shipping_date=date(2026, 6, batch_number),
            created_by=self.user,
        )
        batch = ShipmentBatch.objects.create(
            order=order,
            factory_confirmation=confirmation,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            batch_number=batch_number,
            batch_date=date(2026, 6, batch_number),
            month_key="2026-06",
            shipped_this_batch_quantity=1,
            total_requested_quantity=1,
            total_shipped_after_batch_quantity=1,
            remaining_after_batch_quantity=0,
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=1,
        )
        SerialItem.objects.create(
            factory_confirmation=confirmation,
            order=order,
            product=self.product,
            product_code=self.product.code,
            serial_number=f"SERIAL-{order.id}-{batch_number}",
            expiration_date=date(2028, 1, 1),
        )
        numbers = {
            "invoice_number": current_number,
            "base_invoice_number": current_number.split("-B")[0],
            "batch_number": batch_number,
            "batch_numbering_warnings": [],
        }
        invoice_data = build_batch_hospital_invoice_data(
            batch,
            {"payment_terms_days": 30},
            numbers,
        )
        source_data = {
            "shipment_batch_id": batch.id,
            "numbers": numbers,
            "pricing_basis": {
                "price_basis_type": "hospital_order_date",
                "hospital_order_date": order.order_date.isoformat(),
                "rows": [
                    {
                        "product_code": self.product.code,
                        "hospital_unit_price": "270.0",
                        "hospital_order_date": order.order_date.isoformat(),
                        "line_total": "270.0",
                    }
                ],
            },
            "invoice_data": invoice_data,
        }
        relative_base = Path("production") / f"invoice-{order.id}-{batch_number}"
        html_path = Path(self.media_directory.name) / relative_base.with_suffix(
            ".html"
        )
        pdf_path = Path(self.media_directory.name) / relative_base.with_suffix(
            ".pdf"
        )
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(current_number, encoding="utf-8")
        pdf_path.write_bytes(b"%PDF-production-test")
        document = GeneratedDocument.objects.create(
            order=order,
            shipment_batch=batch,
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            document_number=current_number,
            pdf_file=str(pdf_path.relative_to(self.media_directory.name)),
            html_file=str(html_path.relative_to(self.media_directory.name)),
            source_data=source_data,
            generated_by=self.user,
        )
        workflow = DocumentWorkflowItem.objects.create(
            order=order,
            shipment_batch=batch,
            invoice_document=document,
            invoice_status=DocumentWorkflowItem.DocumentStatus.GENERATED,
        )
        source_data["workflow_item_id"] = workflow.id
        GeneratedDocument.objects.filter(id=document.id).update(
            source_data=source_data
        )
        document.refresh_from_db()
        return document

    @staticmethod
    def _write_test_pdf(html_content, html_path, pdf_path, project_root):
        from weasyprint import HTML

        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html_content, encoding="utf-8")
        HTML(string=html_content).write_pdf(str(pdf_path))

    def _database_state(self):
        return {
            "sequences": list(
                DocumentSequence.objects.order_by("id").values()
            ),
            "documents": list(
                GeneratedDocument.objects.order_by("id").values()
            ),
            "workflows": list(
                DocumentWorkflowItem.objects.order_by("id").values()
            ),
        }

    def _production_fingerprints(self):
        return {
            document.id: {
                "pdf": file_fingerprint(Path(document.pdf_file.path)),
                "html": file_fingerprint(Path(document.html_file.path)),
            }
            for document in (
                self.invoice55,
                self.invoice55_b2,
                self.invoice55_b3,
                self.invoice60,
            )
        }

    def _affected_item(self, document):
        return next(
            item
            for item in build_historical_invoice_numbering_plan()[
                "affected_invoice_items"
            ]
            if item["generated_document_id"] == document.id
        )

    def test_01_plan_does_not_write_database(self):
        before = self._database_state()
        build_historical_invoice_numbering_plan()
        self.assertEqual(before, self._database_state())

    def test_02_plan_does_not_generate_files(self):
        before = self._production_fingerprints()
        build_historical_invoice_numbering_plan()
        self.assertEqual(before, self._production_fingerprints())

    def test_03_default_dry_run_does_not_write_database(self):
        before = self._database_state()
        call_command("rebuild_historical_invoice_numbering", stdout=StringIO())
        self.assertEqual(before, self._database_state())

    def test_04_default_dry_run_preserves_production_files(self):
        before = self._production_fingerprints()
        call_command("rebuild_historical_invoice_numbering", stdout=StringIO())
        self.assertEqual(before, self._production_fingerprints())

    def test_05_prerender_creates_html_and_pdf_under_temporary_root(self):
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
            self.assertTrue(result["html_path"].is_file())
            self.assertTrue(result["pdf_path"].is_file())
            result["html_path"].resolve().relative_to(Path(root).resolve())
            result["pdf_path"].resolve().relative_to(Path(root).resolve())

    def test_06_temporary_html_contains_expected_number(self):
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
            self.assertTrue(result["validations"]["html_number_valid"])
            self.assertIn(
                "Invoice 20260105",
                result["html_path"].read_text(encoding="utf-8"),
            )

    def test_07_temporary_pdf_exists_nonempty_and_is_readable(self):
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
            self.assertGreater(result["pdf_size"], 0)
            self.assertTrue(result["validations"]["pdf_structure_valid"])

    def test_08_temporary_directory_is_cleaned(self):
        with TemporaryDirectory() as parent:
            root = Path(parent) / "run"
            root.mkdir()
            with patch(
                "workflow.services.historical_invoice_numbering_prerender_service."
                "write_invoice_html_and_pdf",
                side_effect=self._write_test_pdf,
            ):
                prerender_historical_invoice(
                    self._affected_item(self.invoice55),
                    root,
                )
            self.assertTrue(any(root.iterdir()))
        self.assertFalse(Path(parent).exists())

    def test_09_generated_document_is_unchanged(self):
        before = self._database_state()["documents"]
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(before, self._database_state()["documents"])

    def test_10_document_sequence_is_unchanged(self):
        before = self._database_state()["sequences"]
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(before, self._database_state()["sequences"])

    def test_11_workflow_item_is_unchanged(self):
        before = self._database_state()["workflows"]
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(before, self._database_state()["workflows"])

    def test_12_source_data_is_unchanged(self):
        before = GeneratedDocument.objects.get(
            id=self.invoice55.id
        ).source_data
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(
            before,
            GeneratedDocument.objects.get(id=self.invoice55.id).source_data,
        )

    def test_13_settlement_service_is_not_called(self):
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ), patch(
            "settlements.services.settlement_auto_service."
            "ensure_settlement_account_for_document"
        ) as settlement:
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        settlement.assert_not_called()

    def test_14_factory_po_is_not_in_plan_or_rendered(self):
        plan = build_historical_invoice_numbering_plan()
        ids = {
            item["generated_document_id"]
            for item in plan["invoice_items"]
        }
        self.assertNotIn(self.factory_po.id, ids)

    def test_15_order_55_expected_number_is_20260105(self):
        item = self._affected_item(self.invoice55)
        self.assertEqual(item["expected_sequence"], 1)
        self.assertEqual(item["expected_document_number"], "Invoice 20260105")

    def test_16_order_150222_expected_number_is_20260505(self):
        item = self._affected_item(self.invoice60)
        self.assertEqual(item["expected_sequence"], 5)
        self.assertEqual(item["expected_document_number"], "Invoice 20260505")

    def test_17_b2_and_b3_suffixes_are_preserved(self):
        self.assertEqual(
            build_batch_invoice_number("Invoice 20260105", 1),
            "Invoice 20260105",
        )
        self.assertEqual(
            build_batch_invoice_number("Invoice 20260105", 2),
            "Invoice 20260105-B2",
        )
        self.assertEqual(
            build_batch_invoice_number("Invoice 20260105", 3),
            "Invoice 20260105-B3",
        )

    def test_18_price_and_quantity_snapshot_are_unchanged(self):
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = prerender_historical_invoice(
                self._affected_item(self.invoice60),
                Path(root),
            )
        self.assertTrue(result["validations"]["business_snapshot_matches"])
        row = result["business_snapshot"]["items"][0]
        self.assertEqual(row["hospital_unit_price"], "270")
        self.assertEqual(row["quantity"], "1")
        self.assertNotEqual(row["hospital_unit_price"], "300")

    def test_19_pricing_basis_is_unchanged(self):
        item = self._affected_item(self.invoice60)
        with TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=self._write_test_pdf,
        ):
            result = prerender_historical_invoice(item, Path(root))
        self.assertEqual(
            item["business_snapshot"]["pricing_basis"],
            result["business_snapshot"]["pricing_basis"],
        )

    def test_20_render_failure_does_not_write_database(self):
        before = self._database_state()
        with self.assertRaises(RuntimeError), TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=RuntimeError("test render failure"),
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(before, self._database_state())

    def test_21_render_failure_preserves_production_files(self):
        before = self._production_fingerprints()
        with self.assertRaises(RuntimeError), TemporaryDirectory() as root, patch(
            "workflow.services.historical_invoice_numbering_prerender_service."
            "write_invoice_html_and_pdf",
            side_effect=RuntimeError("test render failure"),
        ):
            prerender_historical_invoice(
                self._affected_item(self.invoice55),
                Path(root),
            )
        self.assertEqual(before, self._production_fingerprints())

    def test_22_apply_requires_explicit_confirmation(self):
        with self.assertRaisesMessage(
            CommandError,
            "Historical Invoice apply requires --confirm-historical-invoice-renumbering.",
        ):
            call_command(
                "rebuild_historical_invoice_numbering",
                apply=True,
            )

    def test_23_apply_accepts_explicit_confirmation_flag(self):
        backup_root = Path(self.media_directory.name) / "command-backup"
        with patch(
            "workflow.management.commands.rebuild_historical_invoice_numbering."
            "timestamped_backup_root",
            return_value=backup_root,
        ), patch(
            "workflow.management.commands.rebuild_historical_invoice_numbering."
            "backup_historical_invoice_numbering",
            return_value={
                "backup_root": str(backup_root),
                "database_backup_path": str(backup_root / "database.dump"),
                "files_backup_path": str(backup_root / "files"),
                "mapping_path": str(backup_root / "mapping.json"),
            },
        ), patch(
            "workflow.management.commands.rebuild_historical_invoice_numbering."
            "apply_historical_invoice_numbering",
            return_value={
                "document_sequence_update_count": 2,
                "generated_document_update_count": 4,
                "regenerated_invoice_count": 4,
            },
        ):
            call_command(
                "rebuild_historical_invoice_numbering",
                apply=True,
                confirm_historical_invoice_renumbering=True,
                stdout=StringIO(),
            )

    def test_24_repeated_dry_run_output_is_stable(self):
        first = StringIO()
        second = StringIO()
        call_command("rebuild_historical_invoice_numbering", stdout=first)
        call_command("rebuild_historical_invoice_numbering", stdout=second)
        self.assertEqual(first.getvalue(), second.getvalue())

    def test_25_repeated_validate_render_leaves_no_residual_files(self):
        before = self._production_fingerprints()
        for _ in range(2):
            output = StringIO()
            with patch(
                "workflow.services.historical_invoice_numbering_prerender_service."
                "write_invoice_html_and_pdf",
                side_effect=self._write_test_pdf,
            ):
                call_command(
                    "rebuild_historical_invoice_numbering",
                    validate_render=True,
                    stdout=output,
                )
            self.assertIn("temporary_directory_cleaned=True", output.getvalue())
        self.assertEqual(before, self._production_fingerprints())
