from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from documents.models import DocumentSequence, GeneratedDocument
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_document_generation_service import (
    generate_documents_for_workflow_item,
    generate_factory_po_for_workflow_item,
    generate_hospital_invoice_for_workflow_item,
)


class NormalDocumentGenerationTransactionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="normal-generation",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="NORMAL GENERATION FACTORY",
            short_name="NGF",
        )
        department = ProductCategory.objects.create(
            name="NORMAL GENERATION DEPARTMENT",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        factory_node = ProductCategory.objects.create(
            name="NORMAL GENERATION FACTORY NODE",
            parent=department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        category = ProductCategory.objects.create(
            name="NORMAL GENERATION CATEGORY",
            parent=factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product = Product.objects.create(
            code="NORMAL-001",
            description="Normal generation product",
            category=category,
            factory=self.factory,
            hospital_unit_price=Decimal("270.00"),
            factory_unit_price=Decimal("120.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Normal generation price",
            factory=self.factory,
            category=category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("270.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.order = Order.objects.create(
            bon_de_commande="900001",
            order_date=date(2026, 8, 13),
            hospital_name="NORMAL HOSPITAL",
            hospital_order_pdf="hospital_orders/normal.pdf",
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
            hospital_unit_price=Decimal("270.00"),
            price_policy=self.policy,
            price_policy_date=self.order.order_date,
        )
        self.confirmation = FactoryConfirmation.objects.create(
            order=self.order,
            factory=self.factory,
            confirmation_pdf="factory_confirmations/normal.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            shipping_date=date(2026, 8, 14),
            created_by=self.user,
        )
        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            factory_confirmation=self.confirmation,
            batch_number=1,
            batch_date=self.confirmation.shipping_date,
            month_key="2026-08",
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
            serial_number="NORMAL-SERIAL-001",
            expiration_date=date(2028, 1, 1),
        )
        self.item = DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=self.batch,
        )

    def generation_patches(self):
        return (
            patch(
                "workflow.services.workflow_document_generation_service."
                "render_invoice_html",
                return_value="<html>invoice</html>",
            ),
            patch(
                "workflow.services.workflow_document_generation_service."
                "render_po_html",
                return_value="<html>po</html>",
            ),
            patch(
                "workflow.services.workflow_document_generation_service."
                "write_invoice_html_and_pdf",
            ),
            patch(
                "workflow.services.workflow_document_generation_service."
                "write_po_html_and_pdf",
            ),
        )

    def test_normal_invoice_generation_uses_atomic_numbering_and_reuses_it(self):
        invoice_render, po_render, invoice_writer, po_writer = (
            self.generation_patches()
        )
        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ), invoice_render, po_render, invoice_writer as writer, po_writer:
            first = generate_hospital_invoice_for_workflow_item(
                item=self.item,
                generated_by=self.user,
            )
            second = generate_hospital_invoice_for_workflow_item(
                item=self.item,
                generated_by=self.user,
            )

        sequence = DocumentSequence.objects.get(
            bon_de_commande=self.order.bon_de_commande
        )
        self.assertEqual(sequence.sequence, 1)
        self.assertEqual(sequence.invoice_number, "Invoice 20260108")
        self.assertEqual(sequence.po_number, "DELAHK0108S")
        self.assertEqual(DocumentSequence.objects.count(), 1)
        self.assertEqual(first["document_number"], "Invoice 20260108")
        self.assertEqual(
            first["generated_document_id"],
            second["generated_document_id"],
        )
        self.assertTrue(second["reused_existing"])
        self.assertEqual(
            GeneratedDocument.objects.filter(
                document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE
            ).count(),
            1,
        )
        writer.assert_called_once()

    def test_normal_full_generation_has_no_transaction_management_error(self):
        invoice_render, po_render, invoice_writer, po_writer = (
            self.generation_patches()
        )
        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ), invoice_render, po_render as po_render_mock, invoice_writer, (
            po_writer
        ) as po_writer_mock, patch(
            "documents.services.document_numbering_service."
            "get_next_sequence_for_month",
            side_effect=AssertionError("MAX+1 must not be used"),
        ):
            result = generate_documents_for_workflow_item(
                item=self.item,
                generated_by=self.user,
            )

        self.item.refresh_from_db()
        self.assertEqual(
            result["invoice"]["document_number"],
            "Invoice 20260108",
        )
        self.assertEqual(
            result["factory_po"]["document_number"],
            "DELAHK0108S",
        )
        sequence = DocumentSequence.objects.get(
            bon_de_commande=self.order.bon_de_commande
        )
        self.assertEqual(sequence.sequence, 1)
        self.assertEqual(sequence.invoice_number, "Invoice 20260108")
        self.assertEqual(sequence.po_number, "DELAHK0108S")
        self.assertEqual(DocumentSequence.objects.count(), 1)
        po_document = result["factory_po"]["generated_document"]
        self.assertEqual(po_document.document_number, "DELAHK0108S")
        self.assertEqual(
            po_document.source_data["numbers"]["po_number"],
            "DELAHK0108S",
        )
        self.assertIn(
            "DELAHK0108S",
            result["factory_po"]["pdf_path"],
        )
        self.assertIn(
            "DELAHK0108S",
            result["factory_po"]["html_path"],
        )
        self.assertEqual(
            po_render_mock.call_args.kwargs["po_data"]["po"]["po_number"],
            "DELAHK0108S",
        )
        self.assertIn(
            "DELAHK0108S",
            str(po_writer_mock.call_args.kwargs["pdf_path"]),
        )
        self.assertEqual(
            self.item.workflow_status,
            DocumentWorkflowItem.WorkflowStatus.GENERATED,
        )
        self.assertIsNotNone(self.item.invoice_document_id)
        self.assertIsNotNone(self.item.po_document_id)
        self.assertEqual(
            GeneratedDocument.objects.filter(
                shipment_batch=self.batch,
                document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            ).count(),
            1,
        )
        self.assertEqual(
            GeneratedDocument.objects.filter(
                shipment_batch=self.batch,
                document_type=GeneratedDocument.DocumentType.FACTORY_PO,
            ).count(),
            1,
        )
        po_writer_mock.assert_called_once()
    def test_factory_po_fills_existing_blank_number_in_place(self):
        sequence = DocumentSequence.objects.create(
            month_key="2026-08",
            bon_de_commande=self.order.bon_de_commande,
            sequence=1,
            invoice_number="Invoice 20260108",
            po_number="",
        )
        invoice_render, po_render, invoice_writer, po_writer = (
            self.generation_patches()
        )

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ), invoice_render, po_render, invoice_writer, po_writer:
            result = generate_factory_po_for_workflow_item(
                item=self.item,
                generated_by=self.user,
            )

        sequence.refresh_from_db()
        self.assertEqual(sequence.sequence, 1)
        self.assertEqual(sequence.invoice_number, "Invoice 20260108")
        self.assertEqual(sequence.po_number, "DELAHK0108S")
        self.assertEqual(DocumentSequence.objects.count(), 1)
        self.assertEqual(
            DocumentSequence.objects.get().id,
            sequence.id,
        )
        self.assertEqual(result["document_number"], "DELAHK0108S")
        self.assertEqual(
            result["generated_document"].source_data["numbers"]["po_number"],
            "DELAHK0108S",
        )
        self.assertIn("DELAHK0108S", result["pdf_path"])

    def test_factory_po_reuses_existing_nonempty_number(self):
        sequence = DocumentSequence.objects.create(
            month_key="2026-08",
            bon_de_commande=self.order.bon_de_commande,
            sequence=1,
            invoice_number="Invoice 20260108",
            po_number="DELAHK0108S",
        )
        invoice_render, po_render, invoice_writer, po_writer = (
            self.generation_patches()
        )

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ), invoice_render, po_render, invoice_writer, po_writer, patch(
            "documents.services.document_numbering_service.build_po_number",
            side_effect=AssertionError("existing PO number must be reused"),
        ):
            result = generate_factory_po_for_workflow_item(
                item=self.item,
                generated_by=self.user,
            )

        sequence.refresh_from_db()
        self.assertEqual(sequence.sequence, 1)
        self.assertEqual(sequence.invoice_number, "Invoice 20260108")
        self.assertEqual(sequence.po_number, "DELAHK0108S")
        self.assertEqual(DocumentSequence.objects.count(), 1)
        self.assertEqual(
            DocumentSequence.objects.get().id,
            sequence.id,
        )
        self.assertEqual(result["document_number"], "DELAHK0108S")
