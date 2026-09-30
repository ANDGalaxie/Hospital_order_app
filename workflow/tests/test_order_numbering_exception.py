from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TransactionTestCase, override_settings

from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_numbering_service import (
    build_po_number,
    get_or_create_expected_invoice_numbers,
)
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem


class OrderNumberingExceptionCommandTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="numbering-exception",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="EXCEPTION FACTORY",
            short_name="EXF",
        )
        department = ProductCategory.objects.create(
            name="EXCEPTION DEPARTMENT",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        factory_node = ProductCategory.objects.create(
            name="EXCEPTION FACTORY NODE",
            parent=department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        category = ProductCategory.objects.create(
            name="EXCEPTION CATEGORY",
            parent=factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product = Product.objects.create(
            code="EXCEPTION-001",
            description="Exception product",
            category=category,
            factory=self.factory,
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Exception price",
            factory=self.factory,
            category=category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.existing_order = Order.objects.create(
            bon_de_commande="152870",
            order_date=date(2026, 7, 15),
            factory=self.factory,
            created_by=self.user,
        )
        self.existing_sequence = DocumentSequence.objects.create(
            month_key="2026-07",
            bon_de_commande="152870",
            sequence=1,
            invoice_number="Invoice 20260107",
            po_number="DELAHK0107S",
        )
        self.order = Order.objects.create(
            bon_de_commande="152376",
            order_date=date(2026, 7, 7),
            hospital_name="EXCEPTION HOSPITAL",
            hospital_order_pdf="hospital_orders/exception.pdf",
            factory=self.factory,
            created_by=self.user,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=1,
            confirmed_quantity=1,
            backordered_quantity=0,
            hospital_unit_price=Decimal("250.00"),
            price_policy=self.policy,
            price_policy_date=self.order.order_date,
        )
        self.confirmation = FactoryConfirmation.objects.create(
            order=self.order,
            factory=self.factory,
            confirmation_pdf="factory_confirmations/exception.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            shipping_date=date(2026, 9, 1),
            created_by=self.user,
        )
        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            factory_confirmation=self.confirmation,
            batch_number=1,
            batch_date=self.confirmation.shipping_date,
            month_key="2026-09",
            total_requested_quantity=1,
            shipped_this_batch_quantity=1,
            total_shipped_after_batch_quantity=1,
            remaining_after_batch_quantity=0,
        )
        ShipmentBatchItem.objects.create(
            batch=self.batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=1,
        )
        SerialItem.objects.create(
            factory_confirmation=self.confirmation,
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            serial_number="EXCEPTION-SERIAL-001",
            expiration_date=date(2028, 1, 1),
        )
        self.item = DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=self.batch,
        )

    @staticmethod
    def write_fake_pdf(
        *,
        html_content,
        html_path,
        pdf_path,
        **kwargs,
    ):
        html_path = Path(html_path)
        pdf_path = Path(pdf_path)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html_content, encoding="utf-8")
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    def test_without_confirm_is_read_only(self):
        with self.assertRaisesRegex(CommandError, "No changes made"):
            call_command(
                "generate_order_numbering_exception",
                bon="152376",
                sequence=2,
                stdout=StringIO(),
            )

        self.assertFalse(
            DocumentSequence.objects.filter(
                bon_de_commande="152376"
            ).exists()
        )
        self.assertFalse(
            GeneratedDocument.objects.filter(
                order=self.order,
                document_type__in=[
                    GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
                    GeneratedDocument.DocumentType.FACTORY_PO,
                ],
            ).exists()
        )

    def test_confirm_generates_only_the_scoped_exception(self):
        item_snapshot = list(
            self.order.items.values(
                "id",
                "requested_quantity",
                "confirmed_quantity",
                "backordered_quantity",
                "hospital_unit_price",
                "price_policy_id",
                "price_policy_date",
            )
        )
        output = StringIO()

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ), patch(
            "workflow.services.workflow_document_generation_service."
            "render_invoice_html",
            return_value="<html>invoice</html>",
        ), patch(
            "workflow.services.workflow_document_generation_service."
            "render_po_html",
            return_value="<html>po</html>",
        ), patch(
            "workflow.services.workflow_document_generation_service."
            "write_invoice_html_and_pdf",
            side_effect=self.write_fake_pdf,
        ), patch(
            "workflow.services.workflow_document_generation_service."
            "write_po_html_and_pdf",
            side_effect=self.write_fake_pdf,
        ):
            call_command(
                "generate_order_numbering_exception",
                bon="152376",
                sequence=2,
                confirm=True,
                stdout=output,
            )

            invoice = GeneratedDocument.objects.get(
                order=self.order,
                document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            )
            po = GeneratedDocument.objects.get(
                order=self.order,
                document_type=GeneratedDocument.DocumentType.FACTORY_PO,
            )
            self.assertTrue(Path(invoice.pdf_file.path).is_file())
            self.assertTrue(Path(po.pdf_file.path).is_file())

        target_sequence = DocumentSequence.objects.get(
            bon_de_commande="152376"
        )
        self.assertEqual(target_sequence.sequence, 2)
        self.assertEqual(
            target_sequence.invoice_number,
            "Invoice 20260207",
        )
        self.assertEqual(
            target_sequence.po_number,
            build_po_number(self.order.order_date, 2),
        )
        self.assertEqual(
            target_sequence.po_number,
            "DELAHK0207S",
        )
        self.assertEqual(invoice.document_number, "Invoice 20260207")
        self.assertEqual(po.document_number, "DELAHK0207S")
        self.assertEqual(
            invoice.source_data["numbers"]["invoice_number"],
            "Invoice 20260207",
        )
        self.assertEqual(
            po.source_data["numbers"]["po_number"],
            "DELAHK0207S",
        )
        self.assertIn("RESULT order_id=", output.getvalue())

        self.existing_sequence.refresh_from_db()
        self.assertEqual(self.existing_sequence.sequence, 1)
        self.assertEqual(
            self.existing_sequence.invoice_number,
            "Invoice 20260107",
        )
        self.assertEqual(
            self.existing_sequence.po_number,
            "DELAHK0107S",
        )
        self.assertEqual(
            list(
                self.order.items.values(
                    "id",
                    "requested_quantity",
                    "confirmed_quantity",
                    "backordered_quantity",
                    "hospital_unit_price",
                    "price_policy_id",
                    "price_policy_date",
                )
            ),
            item_snapshot,
        )
        frozen = get_or_create_expected_invoice_numbers(self.order)
        self.assertEqual(frozen["sequence"], 2)
        self.assertEqual(frozen["invoice_number"], "Invoice 20260207")
        self.assertEqual(frozen["po_number"], "DELAHK0207S")
        self.assertFalse(frozen["created"])

    def test_sequence_collision_blocks_before_generation(self):
        collision_order = Order.objects.create(
            bon_de_commande="OTHER-JULY",
            order_date=date(2026, 7, 20),
            factory=self.factory,
            created_by=self.user,
        )
        DocumentSequence.objects.create(
            month_key="2026-07",
            bon_de_commande=collision_order.bon_de_commande,
            sequence=2,
            invoice_number="Invoice 20260207",
            po_number="DELAHK0207S",
        )

        with self.assertRaisesRegex(CommandError, "already occupied"):
            call_command(
                "generate_order_numbering_exception",
                bon="152376",
                sequence=2,
                confirm=True,
                stdout=StringIO(),
            )

        self.assertFalse(
            DocumentSequence.objects.filter(
                bon_de_commande="152376"
            ).exists()
        )
