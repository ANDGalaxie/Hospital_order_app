from datetime import date
from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase

from factories.models import Factory
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from products.models import (
    Product,
    ProductCategory,
)
from shipments.models import (
    ShipmentBatch,
    ShipmentBatchItem,
)
from workflow.models import (
    DocumentWorkflowItem,
)
from workflow.services.workflow_price_validation_service import (
    validate_workflow_price_snapshots,
)


class WorkflowPriceValidationTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="workflow-price-test",
                password="test",
            )
        )

        self.factory = Factory.objects.create(
            name="WORKFLOW PRICE FACTORY",
            short_name="WPF",
        )

        self.department = (
            ProductCategory.objects.create(
                name="WORKFLOW PRICE DEPARTMENT",
                node_type=(
                    ProductCategory
                    .NodeType
                    .DEPARTMENT
                ),
            )
        )

        self.factory_node = (
            ProductCategory.objects.create(
                name="WORKFLOW PRICE FACTORY NODE",
                parent=self.department,
                node_type=(
                    ProductCategory
                    .NodeType
                    .FACTORY
                ),
                factory=self.factory,
            )
        )

        self.category = (
            ProductCategory.objects.create(
                name="WORKFLOW PRICE CATEGORY",
                parent=self.factory_node,
                node_type=(
                    ProductCategory
                    .NodeType
                    .CATEGORY
                ),
            )
        )

        self.product = Product.objects.create(
            code="WORKFLOW-PRICE-001",
            description="Workflow price product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal(
                "10.00"
            ),
            factory_unit_price=Decimal(
                "5.00"
            ),
        )

        self.policy = PricePolicy(
            name="Workflow price policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal(
                "250.00"
            ),
            factory_unit_price=Decimal(
                "120.00"
            ),
            expiration_discount_rate=Decimal(
                "0.30"
            ),
            expiration_threshold_days=365,
            is_active=True,
        )

        self.policy.full_clean()
        self.policy.save()

        self.order = Order.objects.create(
            bon_de_commande="WF-PRICE-001",
            hospital_name="TEST HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            created_by=self.user,
        )

        self.order_item = (
            OrderItem.objects.create(
                order=self.order,
                product=self.product,
                product_code=self.product.code,
                requested_quantity=2,
                confirmed_quantity=2,
                hospital_unit_price=Decimal(
                    "250.00"
                ),
                factory_unit_price=Decimal(
                    "120.00"
                ),
                expiration_discount_rate=Decimal(
                    "0.30"
                ),
                expiration_threshold_days=365,
                price_policy=self.policy,
                price_policy_date=date(
                    2026,
                    6,
                    15,
                ),
            )
        )

        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=(
                ShipmentBatch.SourceType.MANUAL
            ),
            batch_number=1,
            batch_date=date(2026, 7, 1),
            month_key="2026-07",
        )

        ShipmentBatchItem.objects.create(
            batch=self.batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=2,
        )

        self.workflow_item = (
            DocumentWorkflowItem.objects.create(
                order=self.order,
                shipment_batch=self.batch,
            )
        )

    def test_complete_snapshot_is_valid(self):
        result = (
            validate_workflow_price_snapshots(
                self.workflow_item
            )
        )

        self.assertTrue(result["is_valid"])
        self.assertEqual(
            result["errors"],
            [],
        )
        self.assertEqual(
            result["checked_product_count"],
            1,
        )

    def test_missing_policy_is_blocking(self):
        self.order_item.price_policy = None
        self.order_item.price_policy_date = None

        self.order_item.save(
            update_fields=[
                "price_policy",
                "price_policy_date",
                "updated_at",
            ]
        )

        result = (
            validate_workflow_price_snapshots(
                self.workflow_item
            )
        )

        self.assertFalse(result["is_valid"])

        error_text = " ".join(
            result["errors"]
        )

        self.assertIn(
            "没有命中 PricePolicy",
            error_text,
        )

        self.assertIn(
            "缺少价格规则日期快照",
            error_text,
        )

    def test_zero_factory_price_is_blocking(self):
        self.order_item.factory_unit_price = (
            Decimal("0.00")
        )

        self.order_item.save(
            update_fields=[
                "factory_unit_price",
                "updated_at",
            ]
        )

        result = (
            validate_workflow_price_snapshots(
                self.workflow_item
            )
        )

        self.assertFalse(result["is_valid"])

        self.assertIn(
            "缺少有效工厂采购单价",
            " ".join(result["errors"]),
        )
