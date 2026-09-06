from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase

from factories.models import Factory
from factory_confirmations.models import (
    FactoryConfirmation,
)
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
from workflow.services.batch_price_snapshot_service import (
    build_batch_price_snapshot,
)


class BatchPriceSnapshotTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="batch-price-test",
                password="test",
            )
        )

        self.factory = Factory.objects.create(
            name="BATCH PRICE FACTORY",
            short_name="BPF",
        )

        self.department = (
            ProductCategory.objects.create(
                name="BATCH PRICE DEPARTMENT",
                node_type=(
                    ProductCategory
                    .NodeType
                    .DEPARTMENT
                ),
            )
        )

        self.factory_node = (
            ProductCategory.objects.create(
                name="BATCH PRICE FACTORY NODE",
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
                name="BATCH PRICE CATEGORY",
                parent=self.factory_node,
                node_type=(
                    ProductCategory
                    .NodeType
                    .CATEGORY
                ),
            )
        )

        self.product = Product.objects.create(
            code="BATCH-PRICE-001",
            description="Batch price product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal(
                "1.00"
            ),
            factory_unit_price=Decimal(
                "1.00"
            ),
        )

        self.policy = PricePolicy(
            name="Batch price policy",
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
            bon_de_commande=(
                "BATCH-PRICE-TEST"
            ),
            hospital_name="TEST HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            order_date=date(2026, 6, 15),
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

        self.confirmation = (
            FactoryConfirmation.objects.create(
                order=self.order,
                factory=self.factory,
                confirmation_pdf=(
                    "factory_confirmations/test.pdf"
                ),
                extraction_status=(
                    FactoryConfirmation
                    .ExtractionStatus
                    .SUCCESS
                ),
                shipping_date=date(2026, 7, 1),
                created_by=self.user,
            )
        )

        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=(
                ShipmentBatch
                .SourceType
                .FACTORY_CONFIRMATION
            ),
            factory_confirmation=(
                self.confirmation
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

    @patch(
        "workflow.services."
        "batch_price_snapshot_service."
        "get_batch_pricing_serial_rows"
    )
    def test_mixed_normal_and_discounted_serials(
        self,
        mock_serial_rows,
    ):
        mock_serial_rows.return_value = [
            {
                "source": "test",
                "source_id": 1,
                "product_code": (
                    self.product.code
                ),
                "serial_number": "SN-NORMAL",
                "expiration_date": date(
                    2027,
                    7,
                    1,
                ),
                "quantity": Decimal("1.00"),
            },
            {
                "source": "test",
                "source_id": 2,
                "product_code": (
                    self.product.code
                ),
                "serial_number": "SN-DISCOUNT",
                "expiration_date": date(
                    2027,
                    6,
                    30,
                ),
                "quantity": Decimal("1.00"),
            },
        ]

        result = build_batch_price_snapshot(
            self.batch
        )

        self.assertTrue(
            result["is_valid"],
            result["errors"],
        )

        self.assertEqual(
            result["base_factory_total"],
            Decimal("240.00"),
        )

        self.assertEqual(
            result["actual_factory_total"],
            Decimal("204.00"),
        )

        self.assertEqual(
            result[
                "factory_discount_savings"
            ],
            Decimal("36.00"),
        )

        row = result["product_rows"][0]

        self.assertEqual(
            row["normal_quantity"],
            Decimal("1.00"),
        )

        self.assertEqual(
            row["discounted_quantity"],
            Decimal("1.00"),
        )

        self.assertEqual(
            row[
                "discounted_factory_unit_price"
            ],
            Decimal("84.00"),
        )

        self.assertEqual(
            row["actual_factory_amount"],
            Decimal("204.00"),
        )

        self.assertEqual(
            len(result["po_groups"]),
            2,
        )

    @patch(
        "workflow.services."
        "batch_price_snapshot_service."
        "get_batch_pricing_serial_rows"
    )
    def test_order_item_factory_snapshot_is_not_used(
        self,
        mock_serial_rows,
    ):
        self.order_item.factory_unit_price = None

        self.order_item.save(
            update_fields=[
                "factory_unit_price",
                "updated_at",
            ]
        )

        mock_serial_rows.return_value = [
            {
                "source": "test",
                "source_id": 1,
                "product_code": (
                    self.product.code
                ),
                "serial_number": "SN-001",
                "expiration_date": date(
                    2027,
                    6,
                    30,
                ),
                "quantity": Decimal("1.00"),
            },
            {
                "source": "test",
                "source_id": 2,
                "product_code": (
                    self.product.code
                ),
                "serial_number": "SN-002",
                "expiration_date": date(
                    2027,
                    7,
                    1,
                ),
                "quantity": Decimal("1.00"),
            },
        ]

        result = build_batch_price_snapshot(
            self.batch
        )

        self.assertTrue(
            result["is_valid"],
            result["errors"],
        )
        self.assertEqual(
            result["po_groups"][0][
                "unit_price"
            ],
            Decimal("120.00"),
        )
