from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from backorders.models import (
    BackorderLine,
    BackorderOrderFolder,
    InventoryBatch,
    InventoryItem,
)
from backorders.services.inventory_service import allocate_inventory_to_order
from backorders.services.inventory_shipment_service import (
    create_shipment_batch_from_inventory_allocation,
)
from factories.models import Factory
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
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
