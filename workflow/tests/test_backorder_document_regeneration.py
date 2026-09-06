from datetime import date
from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from backorders.models import (
    BackorderLine,
    BackorderOrderFolder,
    InventoryAllocation,
    InventoryBatch,
    InventoryItem,
)
from documents.models import GeneratedDocument
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_document_generation_service import (
    generate_factory_po_for_workflow_item,
    get_batch_document_numbers,
)


class BackorderDocumentRegenerationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="regeneration-test",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="REGENERATION FACTORY",
            short_name="RGF",
        )
        department = ProductCategory.objects.create(
            name="REGENERATION DEPARTMENT",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        factory_node = ProductCategory.objects.create(
            name="REGENERATION FACTORY NODE",
            parent=department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        category = ProductCategory.objects.create(
            name="REGENERATION CATEGORY",
            parent=factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product = Product.objects.create(
            code="REGEN-001",
            description="Regeneration product",
            category=category,
            factory=self.factory,
            hospital_unit_price=Decimal("999.00"),
            factory_unit_price=Decimal("999.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Regeneration price",
            factory=self.factory,
            category=category,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 6, 30),
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.shipping_policy = PricePolicy.objects.create(
            name="Regeneration shipping price",
            factory=self.factory,
            category=category,
            start_date=date(2026, 7, 1),
            hospital_unit_price=Decimal("260.00"),
            factory_unit_price=Decimal("150.00"),
            expiration_discount_rate=Decimal("0.40"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.order = Order.objects.create(
            bon_de_commande="REGEN-ORDER",
            order_date=date(2026, 3, 1),
            hospital_name="REGEN HOSPITAL",
            hospital_order_pdf="hospital_orders/regen.pdf",
            factory=self.factory,
            created_by=self.user,
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=10,
            confirmed_quantity=2,
            backordered_quantity=8,
            hospital_unit_price=Decimal("250.00"),
            price_policy=self.policy,
            price_policy_date=date(2026, 3, 1),
        )
        backorder_folder = BackorderOrderFolder.objects.create(
            order=self.order,
            line_count=1,
            remaining_total_quantity=8,
        )
        BackorderLine.objects.create(
            order_folder=backorder_folder,
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=10,
            shipped_quantity=2,
            remaining_quantity=8,
        )

    def create_doc(self, batch, doc_type, number, basis):
        key = (
            "hospital_order_date"
            if doc_type == GeneratedDocument.DocumentType.HOSPITAL_INVOICE
            else "factory_shipping_date"
        )
        return GeneratedDocument.objects.create(
            order=self.order,
            shipment_batch=batch,
            document_type=doc_type,
            document_number=number,
            pdf_file=f"old/{number}.pdf",
            html_file=f"old/{number}.html",
            source_data={
                "pricing_basis": {
                    "price_basis_type": key,
                    key: basis,
                    "rows": [],
                }
            },
            generated_by=self.user,
        )

    def attach_docs(self, item, include_po=True):
        invoice = self.create_doc(
            item.shipment_batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            f"OLD-INV-{item.shipment_batch_id}",
            "2026-07-01",
        )
        po = None
        if include_po:
            po = self.create_doc(
                item.shipment_batch,
                GeneratedDocument.DocumentType.FACTORY_PO,
                f"OLD-PO-{item.shipment_batch_id}",
                "2026-07-01",
            )
        item.invoice_document = invoice
        item.po_document = po
        item.invoice_status = DocumentWorkflowItem.DocumentStatus.GENERATED
        item.po_status = DocumentWorkflowItem.DocumentStatus.GENERATED
        item.workflow_status = DocumentWorkflowItem.WorkflowStatus.GENERATED
        item.save()
        return invoice, po

    def create_factory_batch(
        self,
        shipping_date=date(2026, 7, 27),
        confirmation_type=(
            FactoryConfirmation.ConfirmationType.REPLENISHMENT
        ),
        batch_number=2,
    ):
        confirmation = FactoryConfirmation.objects.create(
            order=self.order,
            factory=self.factory,
            confirmation_type=confirmation_type,
            confirmation_pdf="factory_confirmations/regeneration.pdf",
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
            shipping_date=shipping_date,
            created_by=self.user,
        )
        batch = ShipmentBatch.objects.create(
            order=self.order,
            factory_confirmation=confirmation,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            batch_number=batch_number,
            batch_date=shipping_date,
            month_key="2026-07",
            total_requested_quantity=10,
            shipped_this_batch_quantity=1,
            total_shipped_after_batch_quantity=2,
            remaining_after_batch_quantity=8,
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=1,
        )
        SerialItem.objects.create(
            factory_confirmation=confirmation,
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            serial_number=f"FACTORY-SN-{batch.id}",
            expiration_date=date(2027, 7, 26),
        )
        item = DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=batch,
        )
        return batch, item

    def create_inventory_batch(self, shipping_date=date(2026, 7, 27)):
        inventory_batch = InventoryBatch.objects.create(
            factory=self.factory,
            batch_name="REGEN INVENTORY",
            batch_date=shipping_date,
            extraction_status=InventoryBatch.ExtractionStatus.SUCCESS,
            created_by=self.user,
        )
        allocation = InventoryAllocation.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            quantity_requested=1,
            allocated_count=1,
            status=InventoryAllocation.Status.SHIPMENT_CREATED,
            created_by=self.user,
        )
        InventoryItem.objects.create(
            batch=inventory_batch,
            product=self.product,
            product_code=self.product.code,
            serial_number=f"INVENTORY-SN-{inventory_batch.id}",
            expiration_date=date(2027, 7, 26),
            status=InventoryItem.Status.ALLOCATED,
            allocated_order=self.order,
            allocation=allocation,
        )
        batch = ShipmentBatch.objects.create(
            order=self.order,
            inventory_allocation=allocation,
            source_type=ShipmentBatch.SourceType.INVENTORY_ALLOCATION,
            batch_number=3,
            batch_date=date(2026, 8, 1),
            month_key="2026-08",
            total_requested_quantity=10,
            shipped_this_batch_quantity=1,
            total_shipped_after_batch_quantity=3,
            remaining_after_batch_quantity=7,
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=1,
        )
        item = DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=batch,
        )
        return batch, item, inventory_batch, allocation

    def business_snapshot(self, batch):
        batch.refresh_from_db()
        self.order_item.refresh_from_db()
        result = {
            "batches": ShipmentBatch.objects.count(),
            "workflows": DocumentWorkflowItem.objects.count(),
            "batch_items": list(
                ShipmentBatchItem.objects.filter(batch=batch).values_list(
                    "product_code",
                    "shipped_quantity",
                )
            ),
            "totals": (
                batch.total_requested_quantity,
                batch.shipped_this_batch_quantity,
                batch.total_shipped_after_batch_quantity,
                batch.remaining_after_batch_quantity,
            ),
            "order_quantities": (
                self.order_item.requested_quantity,
                self.order_item.confirmed_quantity,
                self.order_item.backordered_quantity,
            ),
            "inventory": list(
                InventoryItem.objects.values_list(
                    "id",
                    "status",
                    "allocation_id",
                )
            ),
            "backorders": list(
                BackorderLine.objects.values_list(
                    "id",
                    "requested_quantity",
                    "shipped_quantity",
                    "remaining_quantity",
                    "status",
                    "is_active",
                )
            ),
        }
        if batch.inventory_allocation_id:
            allocation = batch.inventory_allocation
            allocation.refresh_from_db()
            result["allocation"] = (
                allocation.quantity_requested,
                allocation.allocated_count,
                allocation.status,
            )
        return result

    def generation_context(self, media_root):
        return (
            override_settings(MEDIA_ROOT=media_root),
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

    def apply_command(self, batch, *extra):
        contexts = None
        with TemporaryDirectory() as media_root:
            contexts = self.generation_context(media_root)
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
                call_command(
                    "regenerate_backorder_documents",
                    "--batch-id",
                    str(batch.id),
                    *extra,
                    "--apply",
                    stdout=StringIO(),
                )


    def run_all_command(self, *extra):
        contexts = None
        with TemporaryDirectory() as media_root:
            contexts = self.generation_context(media_root)
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
                return call_command(
                    "regenerate_backorder_documents",
                    "--all-backorders",
                    *extra,
                    stdout=StringIO(),
                )

    def test_all_backorders_excludes_initial_batch(self):
        initial_batch, initial_item = self.create_factory_batch(
            confirmation_type=(
                FactoryConfirmation.ConfirmationType.INITIAL
            ),
            batch_number=1,
        )
        replenishment_batch, replenishment_item = (
            self.create_factory_batch()
        )
        output = StringIO()
        call_command(
            "regenerate_backorder_documents",
            "--all-backorders",
            stdout=output,
        )
        text = output.getvalue()
        self.assertIn("找到的补发批次数: 1", text)
        self.assertIn(
            f"ShipmentBatch {replenishment_batch.id}",
            text,
        )
        self.assertNotIn(
            f"ShipmentBatch {initial_batch.id}",
            text,
        )

    def test_all_backorders_dry_run_is_read_only(self):
        factory_batch, factory_item = self.create_factory_batch()
        inventory_batch, inventory_item, _, _ = (
            self.create_inventory_batch()
        )
        self.attach_docs(factory_item)
        self.attach_docs(inventory_item)
        before = self.business_snapshot(factory_batch)
        before_inventory = self.business_snapshot(
            inventory_batch
        )
        document_count = GeneratedDocument.objects.count()
        output = StringIO()

        with patch(
            "workflow.services.workflow_document_generation_service."
            "write_invoice_html_and_pdf"
        ) as invoice_writer, patch(
            "workflow.services.workflow_document_generation_service."
            "write_po_html_and_pdf"
        ) as po_writer:
            call_command(
                "regenerate_backorder_documents",
                "--all-backorders",
                stdout=output,
            )

        invoice_writer.assert_not_called()
        po_writer.assert_not_called()
        self.assertIn("找到的补发批次数: 2", output.getvalue())
        self.assertIn("执行模式: DRY-RUN", output.getvalue())
        self.assertEqual(
            GeneratedDocument.objects.count(),
            document_count,
        )
        self.assertEqual(
            self.business_snapshot(factory_batch),
            before,
        )
        self.assertEqual(
            self.business_snapshot(inventory_batch),
            before_inventory,
        )

    def test_all_apply_stops_when_any_batch_is_blocked(self):
        good_batch, good_item = self.create_factory_batch()
        bad_batch, bad_item, _, _ = self.create_inventory_batch(None)
        good_invoice, good_po = self.attach_docs(good_item)
        self.attach_docs(bad_item, include_po=False)
        old_sources = (
            good_invoice.source_data,
            good_po.source_data,
        )
        document_count = GeneratedDocument.objects.count()

        with self.assertRaises(CommandError):
            self.run_all_command("--apply")

        good_invoice.refresh_from_db()
        good_po.refresh_from_db()
        self.assertEqual(
            (good_invoice.source_data, good_po.source_data),
            old_sources,
        )
        self.assertEqual(
            GeneratedDocument.objects.count(),
            document_count,
        )

    def test_all_apply_updates_and_creates_without_duplicates(self):
        factory_batch, factory_item = self.create_factory_batch()
        inventory_batch, inventory_item, _, _ = (
            self.create_inventory_batch()
        )
        factory_invoice, factory_po = self.attach_docs(
            factory_item
        )
        inventory_invoice, _ = self.attach_docs(
            inventory_item,
            include_po=False,
        )
        old_ids = {
            "factory_invoice": factory_invoice.id,
            "factory_po": factory_po.id,
            "inventory_invoice": inventory_invoice.id,
        }
        document_count = GeneratedDocument.objects.count()

        self.run_all_command("--apply")

        factory_item.refresh_from_db()
        inventory_item.refresh_from_db()
        factory_invoice.refresh_from_db()
        factory_po.refresh_from_db()
        inventory_invoice.refresh_from_db()
        inventory_po = inventory_item.po_document
        self.assertEqual(
            factory_item.invoice_document_id,
            old_ids["factory_invoice"],
        )
        self.assertEqual(
            factory_item.po_document_id,
            old_ids["factory_po"],
        )
        self.assertEqual(
            inventory_item.invoice_document_id,
            old_ids["inventory_invoice"],
        )
        self.assertIsNotNone(inventory_po)
        self.assertEqual(
            GeneratedDocument.objects.count(),
            document_count + 1,
        )

        self.run_all_command("--apply")
        self.assertEqual(
            GeneratedDocument.objects.count(),
            document_count + 1,
        )
        factory_item.refresh_from_db()
        inventory_item.refresh_from_db()
        self.assertEqual(
            factory_item.invoice_document_id,
            old_ids["factory_invoice"],
        )
        self.assertEqual(
            factory_item.po_document_id,
            old_ids["factory_po"],
        )
        self.assertEqual(
            inventory_item.invoice_document_id,
            old_ids["inventory_invoice"],
        )

    def test_batch_id_and_all_backorders_are_mutually_exclusive(self):
        batch, item = self.create_factory_batch()
        with self.assertRaises(CommandError):
            call_command(
                "regenerate_backorder_documents",
                "--batch-id",
                str(batch.id),
                "--all-backorders",
            )

    def test_dry_run_changes_nothing(self):
        batch, item = self.create_factory_batch()
        invoice, po = self.attach_docs(item)
        old_sources = (invoice.source_data, po.source_data)
        business = self.business_snapshot(batch)
        count = GeneratedDocument.objects.count()

        out = StringIO()
        with patch(
            "workflow.services.workflow_document_generation_service."
            "write_invoice_html_and_pdf"
        ) as invoice_writer, patch(
            "workflow.services.workflow_document_generation_service."
            "write_po_html_and_pdf"
        ) as po_writer:
            call_command(
                "regenerate_backorder_documents",
                "--batch-id",
                str(batch.id),
                stdout=out,
            )

        invoice_writer.assert_not_called()
        po_writer.assert_not_called()
        invoice.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual((invoice.source_data, po.source_data), old_sources)
        self.assertEqual(GeneratedDocument.objects.count(), count)
        self.assertEqual(self.business_snapshot(batch), business)
        self.assertIn("执行模式: DRY-RUN", out.getvalue())
        self.assertIn("是否允许执行: 是", out.getvalue())

    def test_factory_apply_overwrites_same_ids_and_new_basis(self):
        batch, item = self.create_factory_batch()
        invoice, po = self.attach_docs(item)
        old_ids = (invoice.id, po.id)
        business = self.business_snapshot(batch)
        count = GeneratedDocument.objects.count()

        self.apply_command(batch, "--document-type", "all")

        item.refresh_from_db()
        invoice.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(
            (item.invoice_document_id, item.po_document_id),
            old_ids,
        )
        self.assertEqual(GeneratedDocument.objects.count(), count)
        self.assertEqual(
            invoice.source_data["pricing_basis"]["hospital_order_date"],
            "2026-03-01",
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date"],
            "2026-07-27",
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date_source"],
            "factory_confirmation.shipping_date",
        )
        po_row = po.source_data["pricing_basis"]["rows"][0]
        self.assertEqual(
            Decimal(po_row["factory_base_unit_price"]),
            Decimal("150.00"),
        )
        self.assertEqual(
            Decimal(po_row["discount_rate"]),
            Decimal("0.40"),
        )
        self.assertEqual(self.business_snapshot(batch), business)

    def test_inventory_auto_date_overwrites_same_documents(self):
        batch, item, inventory_batch, allocation = self.create_inventory_batch()
        invoice, po = self.attach_docs(item)
        old_ids = (invoice.id, po.id)
        business = self.business_snapshot(batch)

        self.apply_command(batch)

        item.refresh_from_db()
        invoice.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(
            (item.invoice_document_id, item.po_document_id),
            old_ids,
        )
        self.assertEqual(
            invoice.source_data["pricing_basis"]["hospital_order_date"],
            "2026-03-01",
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date"],
            "2026-07-27",
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date_source"],
            "inventory_item.inventory_batch.batch_date",
        )
        po_row = po.source_data["pricing_basis"]["rows"][0]
        self.assertEqual(
            Decimal(po_row["factory_base_unit_price"]),
            Decimal("150.00"),
        )
        self.assertEqual(
            Decimal(po_row["discount_rate"]),
            Decimal("0.40"),
        )
        self.assertEqual(self.business_snapshot(batch), business)

    def test_inventory_missing_date_blocks_then_manual_allows_po(self):
        batch, item, inventory_batch, allocation = self.create_inventory_batch(None)
        self.attach_docs(item, include_po=False)
        business = self.business_snapshot(batch)
        document_count = (
            GeneratedDocument.objects.count()
        )

        out = StringIO()
        call_command(
            "regenerate_backorder_documents",
            "--batch-id",
            str(batch.id),
            "--document-type",
            "purchase_order",
            stdout=out,
        )
        self.assertIn(
            "当前库存补发批次没有可用的工厂实际发货日期",
            out.getvalue(),
        )
        self.assertIn("是否允许执行: 否", out.getvalue())

        with self.assertRaises(CommandError):
            call_command(
                "regenerate_backorder_documents",
                "--batch-id",
                str(batch.id),
                "--document-type",
                "purchase_order",
                "--apply",
                stdout=StringIO(),
                stderr=StringIO(),
            )
        self.assertEqual(self.business_snapshot(batch), business)

        self.apply_command(
            batch,
            "--document-type",
            "purchase_order",
            "--factory-shipping-date",
            "2026-07-27",
        )
        item.refresh_from_db()
        po = item.po_document
        self.assertIsNotNone(po)
        self.assertEqual(
            GeneratedDocument.objects.count(),
            document_count + 1,
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date"],
            "2026-07-27",
        )
        self.assertEqual(
            po.source_data["pricing_basis"]["factory_shipping_date_source"],
            "command_line.factory_shipping_date",
        )
        self.assertEqual(self.business_snapshot(batch), business)

    def test_factory_missing_shipping_date_blocks_po(self):
        batch, item = self.create_factory_batch(None)
        self.attach_docs(item)
        out = StringIO()
        call_command(
            "regenerate_backorder_documents",
            "--batch-id",
            str(batch.id),
            "--document-type",
            "purchase_order",
            stdout=out,
        )
        self.assertIn("FactoryConfirmation.shipping_date 为空", out.getvalue())
        self.assertIn("是否允许执行: 否", out.getvalue())

    def test_normal_generation_still_reuses_existing_po(self):
        batch, item = self.create_factory_batch()
        invoice, po = self.attach_docs(item)
        po.document_number = get_batch_document_numbers(
            batch
        )["po_number"]
        po.save(update_fields=["document_number"])
        old_source = po.source_data

        with patch(
            "workflow.services.workflow_document_generation_service."
            "write_po_html_and_pdf"
        ) as writer:
            result = generate_factory_po_for_workflow_item(
                item=item,
                generated_by=self.user,
            )

        self.assertTrue(result["reused_existing"])
        writer.assert_not_called()
        po.refresh_from_db()
        self.assertEqual(po.source_data, old_source)
