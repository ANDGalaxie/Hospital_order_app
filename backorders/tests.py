from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from backorders.models import (
    BackorderLine,
    BackorderOrderFolder,
    InventoryBatch,
    InventoryAllocation,
    InventoryItem,
)
from backorders.services.inventory_service import allocate_inventory_to_order
from backorders.services.inventory_shipment_service import (
    create_shipment_batch_from_inventory_allocation,
)
from factory_confirmations.models import FactoryConfirmation
from factories.models import Factory
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch, ShipmentBatchItem
from shipments.services.shipment_history_service import rebuild_order_shipment_history
from workflow.models import DocumentWorkflowItem
from documents.models import GeneratedDocument
from workflow.services.workflow_document_generation_service import (
    generate_documents_for_workflow_items,
)
from workflow.services.workflow_sync_service import (
    sync_document_workflow_item_for_batch,
)


class InventoryShipmentWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="inventory-workflow-user",
            password="test",
        )

        self.factory = Factory.objects.create(
            name="Inventory Workflow Factory",
            short_name="IWF",
        )

        self.department = ProductCategory.objects.create(
            name="Inventory Workflow Department",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        self.factory_node = ProductCategory.objects.create(
            name="Inventory Workflow Factory Node",
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        self.category = ProductCategory.objects.create(
            name="Inventory Workflow Category",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )

        self.product = Product.objects.create(
            code="INV-WF-001",
            description="Inventory workflow product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
        )

        self.policy = PricePolicy.objects.create(
            name="Inventory workflow policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
            expiration_discount_rate=Decimal("0.10"),
            expiration_threshold_days=365,
            is_active=True,
        )

        self.order = Order.objects.create(
            bon_de_commande="INV-WF-ORDER-1",
            order_date=date(2026, 6, 1),
            hospital_name="Inventory Workflow Hospital",
            hospital_order_pdf="hospital_orders/test.pdf",
            factory=self.factory,
            created_by=self.user,
        )

        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=2,
            confirmed_quantity=0,
            backordered_quantity=2,
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
            expiration_discount_rate=Decimal("0.10"),
            expiration_threshold_days=365,
            price_policy=self.policy,
            price_policy_date=date(2026, 6, 1),
        )

        self.backorder_folder = BackorderOrderFolder.objects.create(
            order=self.order,
            line_count=1,
            remaining_total_quantity=2,
        )

        self.backorder_line = BackorderLine.objects.create(
            order_folder=self.backorder_folder,
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            description=self.product.description,
            requested_quantity=2,
            shipped_quantity=0,
            remaining_quantity=2,
        )

        self.inventory_batch = InventoryBatch.objects.create(
            factory=self.factory,
            batch_name="Inventory Batch 1",
            batch_date=date(2026, 7, 1),
            created_by=self.user,
            extraction_status=InventoryBatch.ExtractionStatus.SUCCESS,
        )

        InventoryItem.objects.create(
            batch=self.inventory_batch,
            product=self.product,
            product_code=self.product.code,
            serial_number="INV-SERIAL-001",
            expiration_date=date(2027, 7, 1),
            status=InventoryItem.Status.AVAILABLE,
        )
        InventoryItem.objects.create(
            batch=self.inventory_batch,
            product=self.product,
            product_code=self.product.code,
            serial_number="INV-SERIAL-002",
            expiration_date=date(2027, 7, 2),
            status=InventoryItem.Status.AVAILABLE,
        )

    def test_inventory_shipment_creates_single_workflow_item(self):
        allocation = allocate_inventory_to_order(
            product_code=self.product.code,
            order=self.order,
            quantity=2,
            created_by=self.user,
        )

        batch = create_shipment_batch_from_inventory_allocation(
            allocation
        )

        workflow_item = DocumentWorkflowItem.objects.get(
            shipment_batch=batch,
        )

        self.assertEqual(
            DocumentWorkflowItem.objects.filter(
                shipment_batch=batch,
            ).count(),
            1,
        )
        self.assertEqual(
            workflow_item.order_id,
            self.order.id,
        )
        self.assertEqual(
            workflow_item.shipment_batch.inventory_allocation_id,
            allocation.id,
        )
        self.assertIn(
            workflow_item.validation_status,
            [
                DocumentWorkflowItem.ValidationStatus.READY,
                DocumentWorkflowItem.ValidationStatus.NEEDS_REVIEW,
            ],
        )

        sync_document_workflow_item_for_batch(batch)

        self.assertEqual(
            DocumentWorkflowItem.objects.filter(
                shipment_batch=batch,
            ).count(),
            1,
        )

    def test_inventory_shipment_validation_does_not_require_factory_confirmation(self):
        allocation = allocate_inventory_to_order(
            product_code=self.product.code,
            order=self.order,
            quantity=2,
            created_by=self.user,
        )

        batch = create_shipment_batch_from_inventory_allocation(
            allocation
        )
        workflow_item = DocumentWorkflowItem.objects.get(
            shipment_batch=batch,
        )

        errors = (
            (workflow_item.validation_data or {}).get("errors")
            or []
        )
        joined_errors = " ".join(errors)

        self.assertNotIn(
            "FactoryConfirmation",
            joined_errors,
        )

    def test_inventory_workflow_generation_skips_factory_po(self):
        allocation = allocate_inventory_to_order(
            product_code=self.product.code,
            order=self.order,
            quantity=2,
            created_by=self.user,
        )
        batch = create_shipment_batch_from_inventory_allocation(
            allocation
        )
        workflow_item = DocumentWorkflowItem.objects.get(
            shipment_batch=batch,
        )

        invoice_document = GeneratedDocument.objects.create(
            order=self.order,
            shipment_batch=batch,
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            document_number="INV-WF-INVOICE-1",
            generated_by=self.user,
        )

        with self.settings(MEDIA_ROOT="/tmp"):
            from unittest.mock import patch

            with patch(
                "workflow.services.workflow_document_generation_service.generate_hospital_invoice_for_workflow_item",
                return_value={
                    "generated_document": invoice_document,
                    "generated_document_id": invoice_document.id,
                    "document_number": invoice_document.document_number,
                    "warnings": [],
                },
            ):
                summary = generate_documents_for_workflow_items(
                    DocumentWorkflowItem.objects.filter(id=workflow_item.id),
                    generated_by=self.user,
                )

        workflow_item.refresh_from_db()

        self.assertEqual(summary["success"], 1)
        self.assertEqual(
            workflow_item.workflow_status,
            DocumentWorkflowItem.WorkflowStatus.GENERATED,
        )
        self.assertEqual(
            workflow_item.po_status,
            DocumentWorkflowItem.DocumentStatus.GENERATED,
        )
        self.assertFalse(
            GeneratedDocument.objects.filter(
                shipment_batch=batch,
                document_type=GeneratedDocument.DocumentType.FACTORY_PO,
            ).exists()
        )


class ShipmentHistoryRebuildTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="shipment-history-user",
            password="test",
        )

        self.factory = Factory.objects.create(
            name="Shipment History Factory",
            short_name="SHF",
        )

        self.department = ProductCategory.objects.create(
            name="Shipment History Department",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        self.factory_node = ProductCategory.objects.create(
            name="Shipment History Factory Node",
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        self.category = ProductCategory.objects.create(
            name="Shipment History Category",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )

    def create_product(self, code, hospital_price="50.00", factory_price="20.00"):
        return Product.objects.create(
            code=code,
            description=f"Product {code}",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal(hospital_price),
            factory_unit_price=Decimal(factory_price),
        )

    def create_order(self, bon_de_commande):
        return Order.objects.create(
            bon_de_commande=bon_de_commande,
            hospital_name="Shipment History Hospital",
            hospital_order_pdf="hospital_orders/test.pdf",
            factory=self.factory,
            created_by=self.user,
        )

    def add_order_item(self, order, product, requested_quantity):
        return OrderItem.objects.create(
            order=order,
            product=product,
            product_code=product.code,
            requested_quantity=requested_quantity,
            confirmed_quantity=0,
            backordered_quantity=requested_quantity,
            hospital_unit_price=product.hospital_unit_price,
            factory_unit_price=product.factory_unit_price,
        )

    def create_factory_batch(
        self,
        order,
        product,
        quantity,
        batch_number,
        batch_date,
        confirmation_type,
        created_at=None,
    ):
        confirmation = FactoryConfirmation.objects.create(
            order=order,
            factory=self.factory,
            confirmation_pdf="factory_confirmations/test.pdf",
            confirmation_type=confirmation_type,
            shipping_date=batch_date,
            created_by=self.user,
        )
        batch = ShipmentBatch.objects.create(
            order=order,
            factory_confirmation=confirmation,
            source_type=ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
            batch_number=batch_number,
            batch_date=batch_date,
            month_key=batch_date.strftime("%Y-%m"),
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=product,
            product_code=product.code,
            shipped_quantity=quantity,
        )
        if created_at:
            ShipmentBatch.objects.filter(id=batch.id).update(created_at=created_at)
            batch.refresh_from_db()
        return batch

    def create_inventory_batch(
        self,
        order,
        product,
        quantity,
        batch_number,
        batch_date,
        created_at=None,
    ):
        allocation = InventoryAllocation.objects.create(
            order=order,
            product=product,
            product_code=product.code,
            quantity_requested=quantity,
            allocated_count=quantity,
            status=InventoryAllocation.Status.SHIPMENT_CREATED,
            created_by=self.user,
        )
        batch = ShipmentBatch.objects.create(
            order=order,
            inventory_allocation=allocation,
            source_type=ShipmentBatch.SourceType.INVENTORY_ALLOCATION,
            batch_number=batch_number,
            batch_date=batch_date,
            month_key=batch_date.strftime("%Y-%m"),
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=product,
            product_code=product.code,
            shipped_quantity=quantity,
        )
        if created_at:
            ShipmentBatch.objects.filter(id=batch.id).update(created_at=created_at)
            batch.refresh_from_db()
        return batch

    def test_rebuild_order_shipment_history_uses_mixed_sources(self):
        order = self.create_order("SHIP-HISTORY-175")
        product = self.create_product("SHIP-HISTORY-001")
        self.add_order_item(order, product, 175)

        batch_date = date(2026, 7, 1)
        quantities = [106, 44, 5, 5, 4, 2, 4]
        expected_shipped = [106, 150, 155, 160, 164, 166, 170]
        expected_remaining = [69, 25, 20, 15, 11, 9, 5]

        self.create_factory_batch(
            order,
            product,
            106,
            batch_number=1,
            batch_date=batch_date,
            confirmation_type=FactoryConfirmation.ConfirmationType.INITIAL,
            created_at=timezone.make_aware(datetime(2026, 7, 1, 9, 0, 0)),
        )
        self.create_factory_batch(
            order,
            product,
            44,
            batch_number=2,
            batch_date=batch_date,
            confirmation_type=FactoryConfirmation.ConfirmationType.REPLENISHMENT,
            created_at=timezone.make_aware(datetime(2026, 7, 1, 10, 0, 0)),
        )

        created_at = timezone.make_aware(datetime(2026, 7, 1, 11, 0, 0))
        for index, quantity in enumerate([5, 5, 4, 2, 4], start=3):
            self.create_inventory_batch(
                order,
                product,
                quantity,
                batch_number=index,
                batch_date=batch_date,
                created_at=created_at,
            )
            created_at += timedelta(minutes=1)

        rebuild_order_shipment_history(order)

        batches = list(
            ShipmentBatch.objects.filter(order=order).order_by(
                "batch_date", "created_at", "id"
            )
        )

        self.assertEqual(
            [batch.shipped_this_batch_quantity for batch in batches],
            quantities,
        )
        self.assertEqual(
            [batch.total_shipped_after_batch_quantity for batch in batches],
            expected_shipped,
        )
        self.assertEqual(
            [batch.remaining_after_batch_quantity for batch in batches],
            expected_remaining,
        )

        order_item = order.items.get(product_code=product.code)
        self.assertEqual(order_item.confirmed_quantity, 170)
        self.assertEqual(order_item.backordered_quantity, 5)

    def test_rebuild_order_shipment_history_tracks_remaining_by_product(self):
        order = self.create_order("SHIP-HISTORY-MULTI")
        product_a = self.create_product("SHIP-MULTI-A")
        product_b = self.create_product("SHIP-MULTI-B")
        self.add_order_item(order, product_a, 10)
        self.add_order_item(order, product_b, 5)

        batch_date = date(2026, 7, 2)
        batch = self.create_factory_batch(
            order,
            product_a,
            6,
            batch_number=1,
            batch_date=batch_date,
            confirmation_type=FactoryConfirmation.ConfirmationType.INITIAL,
            created_at=timezone.make_aware(datetime(2026, 7, 2, 9, 0, 0)),
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=product_b,
            product_code=product_b.code,
            shipped_quantity=1,
        )

        rebuild_order_shipment_history(order)

        batch.refresh_from_db()
        snapshots = {
            item.product_code: item
            for item in batch.backorder_items.order_by("product_code")
        }
        self.assertEqual(batch.total_shipped_after_batch_quantity, 7)
        self.assertEqual(batch.remaining_after_batch_quantity, 8)
        self.assertEqual(snapshots[product_a.code].remaining_quantity, 4)
        self.assertEqual(snapshots[product_b.code].remaining_quantity, 4)

    def test_rebuild_order_shipment_history_is_idempotent_and_stable(self):
        order = self.create_order("SHIP-HISTORY-STABLE")
        product = self.create_product("SHIP-STABLE-001")
        self.add_order_item(order, product, 20)

        batch_date = date(2026, 7, 3)
        first_batch = self.create_inventory_batch(
            order,
            product,
            5,
            batch_number=1,
            batch_date=batch_date,
            created_at=timezone.make_aware(datetime(2026, 7, 3, 8, 0, 0)),
        )
        second_batch = self.create_inventory_batch(
            order,
            product,
            7,
            batch_number=2,
            batch_date=batch_date,
            created_at=timezone.make_aware(datetime(2026, 7, 3, 8, 0, 0)),
        )

        first_result = rebuild_order_shipment_history(order)
        second_result = rebuild_order_shipment_history(order)

        first_batch.refresh_from_db()
        second_batch.refresh_from_db()

        self.assertEqual(
            [first_batch.total_shipped_after_batch_quantity, second_batch.total_shipped_after_batch_quantity],
            [5, 12],
        )
        self.assertEqual(
            [first_batch.remaining_after_batch_quantity, second_batch.remaining_after_batch_quantity],
            [15, 8],
        )
        self.assertEqual(first_result["batches"], second_result["batches"])
        self.assertEqual(
            first_batch.backorder_items.count(),
            1,
        )
        self.assertEqual(
            second_batch.backorder_items.count(),
            1,
        )

    def test_rebuild_order_shipment_history_preserves_over_shipped_totals(self):
        order = self.create_order("SHIP-HISTORY-OVERSHIP")
        product = self.create_product("SHIP-OVER-001")
        self.add_order_item(order, product, 10)

        batch = self.create_factory_batch(
            order,
            product,
            12,
            batch_number=1,
            batch_date=date(2026, 7, 4),
            confirmation_type=FactoryConfirmation.ConfirmationType.INITIAL,
            created_at=timezone.make_aware(datetime(2026, 7, 4, 9, 0, 0)),
        )

        result = rebuild_order_shipment_history(order)
        order_item = order.items.get(product_code=product.code)
        snapshot = batch.backorder_items.get(product_code=product.code)

        batch.refresh_from_db()

        self.assertEqual(batch.total_shipped_after_batch_quantity, 12)
        self.assertEqual(batch.remaining_after_batch_quantity, 0)
        self.assertEqual(batch.status, ShipmentBatch.Status.OVER_SHIPPED)
        self.assertEqual(order_item.confirmed_quantity, 12)
        self.assertEqual(order_item.backordered_quantity, 0)
        self.assertTrue(snapshot.is_over_shipped)
        self.assertTrue(result["warnings"])
