from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from backorders.models import InventoryAllocation
from factories.models import Factory
from factory_confirmations.models import (
    FactoryConfirmation,
    SerialItem,
)
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from pricing.services.price_policy_service import (
    apply_price_policy_to_order,
)
from products.models import Product, ProductCategory
from shipments.models import (
    ShipmentBatch,
    ShipmentBatchItem,
)
from workflow.models import DocumentWorkflowItem
from workflow.services.batch_price_snapshot_service import (
    build_batch_price_snapshot,
)
from workflow.services.workflow_document_generation_service import (
    build_batch_invoice_items,
    generate_factory_po_for_workflow_item,
    generate_hospital_invoice_for_workflow_item,
)
from workflow.services.workflow_price_validation_service import (
    validate_workflow_price_snapshots,
)


class PricingReferenceDateRegressionTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="pricing-reference-date",
                password="test",
            )
        )
        self.factory = Factory.objects.create(
            name="REFERENCE DATE FACTORY",
            short_name="RDF",
        )
        department = (
            ProductCategory.objects.create(
                name="REFERENCE DEPARTMENT",
                node_type=(
                    ProductCategory
                    .NodeType
                    .DEPARTMENT
                ),
            )
        )
        factory_node = (
            ProductCategory.objects.create(
                name="REFERENCE FACTORY NODE",
                parent=department,
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
                name="REFERENCE CATEGORY",
                parent=factory_node,
                node_type=(
                    ProductCategory
                    .NodeType
                    .CATEGORY
                ),
            )
        )
        self.product = Product.objects.create(
            code="REFERENCE-001",
            description="Reference date product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("999.00"),
            factory_unit_price=Decimal("999.00"),
        )
        self.early_policy = self.create_policy(
            name="Early price",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 5, 31),
            hospital_price="250.00",
            factory_price="100.00",
        )
        self.late_policy = self.create_policy(
            name="Late price",
            start_date=date(2026, 6, 1),
            end_date=None,
            hospital_price="300.00",
            factory_price="140.00",
        )
        self.order = Order.objects.create(
            bon_de_commande="REFERENCE-ORDER",
            order_date=date(2026, 3, 1),
            hospital_name="REFERENCE HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/reference.pdf"
            ),
            factory=self.factory,
            created_by=self.user,
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=20,
            hospital_unit_price=Decimal("0.00"),
        )
        result = apply_price_policy_to_order(
            self.order
        )
        self.assertEqual(result["errors"], [])
        self.order_item.refresh_from_db()
        self.serial_index = 0

    def create_policy(
        self,
        *,
        name,
        start_date,
        end_date,
        hospital_price,
        factory_price,
    ):
        policy = PricePolicy(
            name=name,
            factory=self.factory,
            category=self.category,
            start_date=start_date,
            end_date=end_date,
            hospital_unit_price=Decimal(
                hospital_price
            ),
            factory_unit_price=Decimal(
                factory_price
            ),
            expiration_discount_rate=Decimal(
                "0.30"
            ),
            expiration_threshold_days=365,
            is_active=True,
        )
        policy.full_clean()
        policy.save()
        return policy

    def create_factory_batch(
        self,
        shipping_date,
        expiration_dates,
        batch_number,
    ):
        confirmation = (
            FactoryConfirmation.objects.create(
                order=self.order,
                factory=self.factory,
                confirmation_pdf=(
                    "factory_confirmations/"
                    f"reference-{batch_number}.pdf"
                ),
                extraction_status=(
                    FactoryConfirmation
                    .ExtractionStatus
                    .SUCCESS
                ),
                shipping_date=shipping_date,
                created_by=self.user,
            )
        )
        batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=(
                ShipmentBatch
                .SourceType
                .FACTORY_CONFIRMATION
            ),
            factory_confirmation=confirmation,
            batch_number=batch_number,
            batch_date=shipping_date,
            month_key=shipping_date.strftime(
                "%Y-%m"
            ),
        )
        ShipmentBatchItem.objects.create(
            batch=batch,
            product=self.product,
            product_code=self.product.code,
            shipped_quantity=len(
                expiration_dates
            ),
        )

        for expiration_date in expiration_dates:
            self.serial_index += 1
            SerialItem.objects.create(
                factory_confirmation=confirmation,
                order=self.order,
                product=self.product,
                product_code=self.product.code,
                serial_number=(
                    f"REFERENCE-SN-"
                    f"{self.serial_index}"
                ),
                expiration_date=expiration_date,
            )

        return batch

    def test_replenishment_invoice_keeps_order_date_price(
        self,
    ):
        first = self.create_factory_batch(
            date(2026, 3, 15),
            [date(2028, 1, 1)],
            1,
        )
        replenishment = self.create_factory_batch(
            date(2026, 7, 27),
            [date(2028, 1, 2)],
            2,
        )

        first_items, _ = (
            build_batch_invoice_items(first)
        )
        replenishment_items, _ = (
            build_batch_invoice_items(
                replenishment
            )
        )

        for row in (
            first_items[0],
            replenishment_items[0],
        ):
            self.assertEqual(
                row["hospital_unit_price"],
                "250.00",
            )
            self.assertEqual(
                row["hospital_order_date"],
                "2026-03-01",
            )
            self.assertEqual(
                row["price_basis_type"],
                "hospital_order_date",
            )

    def test_each_po_uses_its_own_shipping_date_price(
        self,
    ):
        first = self.create_factory_batch(
            date(2026, 3, 15),
            [date(2028, 1, 1)],
            1,
        )
        replenishment = self.create_factory_batch(
            date(2026, 7, 27),
            [date(2028, 1, 2)],
            2,
        )

        first_snapshot = (
            build_batch_price_snapshot(first)
        )
        second_snapshot = (
            build_batch_price_snapshot(
                replenishment
            )
        )

        self.assertEqual(
            first_snapshot["po_groups"][0][
                "unit_price"
            ],
            Decimal("100.00"),
        )
        self.assertEqual(
            second_snapshot["po_groups"][0][
                "unit_price"
            ],
            Decimal("140.00"),
        )
        self.assertEqual(
            first_snapshot["reference_date"],
            date(2026, 3, 15),
        )
        self.assertEqual(
            second_snapshot["reference_date"],
            date(2026, 7, 27),
        )

    def test_shipping_date_controls_discount_and_grouping(
        self,
    ):
        batch = self.create_factory_batch(
            date(2026, 7, 27),
            [
                date(2027, 7, 27),
                date(2027, 7, 26),
            ],
            1,
        )

        snapshot = build_batch_price_snapshot(
            batch
        )

        self.assertTrue(
            snapshot["is_valid"],
            snapshot["errors"],
        )
        self.assertEqual(
            len(snapshot["po_groups"]),
            2,
        )
        self.assertEqual(
            {
                group["discount_rate"]
                for group in snapshot["po_groups"]
            },
            {
                Decimal("0.00"),
                Decimal("0.3000"),
            },
        )
        self.assertTrue(
            all(
                group["price_basis_type"]
                == "factory_shipping_date"
                for group in snapshot[
                    "po_groups"
                ]
            )
        )

    def test_same_expiration_can_change_discount_by_batch(
        self,
    ):
        expiration = date(2027, 5, 1)
        early = self.create_factory_batch(
            date(2026, 3, 15),
            [expiration],
            1,
        )
        late = self.create_factory_batch(
            date(2026, 7, 27),
            [expiration],
            2,
        )

        early_group = (
            build_batch_price_snapshot(
                early
            )["po_groups"][0]
        )
        late_group = (
            build_batch_price_snapshot(
                late
            )["po_groups"][0]
        )

        self.assertEqual(
            early_group["discount_rate"],
            Decimal("0.00"),
        )
        self.assertEqual(
            late_group["discount_rate"],
            Decimal("0.3000"),
        )

    def test_missing_or_mismatched_business_dates_block(
        self,
    ):
        batch = self.create_factory_batch(
            date(2026, 7, 27),
            [date(2028, 1, 1)],
            1,
        )
        workflow_item = (
            DocumentWorkflowItem.objects.create(
                order=self.order,
                shipment_batch=batch,
            )
        )

        self.order.order_date = None
        self.order.save(
            update_fields=[
                "order_date",
                "updated_at",
            ]
        )
        result = (
            validate_workflow_price_snapshots(
                workflow_item
            )
        )
        self.assertIn(
            "Order.order_date 为空",
            " ".join(result["errors"]),
        )

        self.order.order_date = date(
            2026,
            3,
            1,
        )
        self.order.save(
            update_fields=[
                "order_date",
                "updated_at",
            ]
        )
        batch.batch_date = date(2026, 7, 28)
        batch.save(
            update_fields=[
                "batch_date",
                "updated_at",
            ]
        )
        result = (
            validate_workflow_price_snapshots(
                workflow_item
            )
        )
        self.assertIn(
            "不一致",
            " ".join(result["errors"]),
        )

        batch.factory_confirmation.shipping_date = (
            None
        )
        batch.factory_confirmation.save(
            update_fields=[
                "shipping_date",
                "updated_at",
            ]
        )
        result = (
            validate_workflow_price_snapshots(
                workflow_item
            )
        )
        self.assertIn(
            "FactoryConfirmation.shipping_date 为空",
            " ".join(result["errors"]),
        )

    def test_inventory_replenishment_uses_invoice_snapshot_and_skips_po(
        self,
    ):
        allocation = (
            InventoryAllocation.objects.create(
                order=self.order,
                product=self.product,
                product_code=self.product.code,
                quantity_requested=1,
                allocated_count=1,
                status=(
                    InventoryAllocation
                    .Status
                    .SHIPMENT_CREATED
                ),
                created_by=self.user,
            )
        )
        batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=(
                ShipmentBatch
                .SourceType
                .INVENTORY_ALLOCATION
            ),
            inventory_allocation=allocation,
            batch_number=3,
            batch_date=date(2026, 8, 1),
            month_key="2026-08",
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

        invoice_items, _ = (
            build_batch_invoice_items(batch)
        )
        po_result = (
            generate_factory_po_for_workflow_item(
                item=item,
                generated_by=self.user,
            )
        )

        self.assertEqual(
            invoice_items[0][
                "hospital_unit_price"
            ],
            "250.00",
        )
        self.assertTrue(
            po_result["skipped"]
        )

    def test_generated_document_reuses_complete_pricing_snapshot(
        self,
    ):
        batch = self.create_factory_batch(
            date(2026, 7, 27),
            [
                date(2027, 7, 27),
                date(2027, 7, 26),
            ],
            1,
        )
        item = DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=batch,
        )

        with TemporaryDirectory() as media_root:
            with override_settings(
                MEDIA_ROOT=media_root
            ), patch(
                "workflow.services."
                "workflow_document_generation_service."
                "render_invoice_html",
                return_value="<html></html>",
            ), patch(
                "workflow.services."
                "workflow_document_generation_service."
                "render_po_html",
                return_value="<html></html>",
            ), patch(
                "workflow.services."
                "workflow_document_generation_service."
                "write_invoice_html_and_pdf",
            ), patch(
                "workflow.services."
                "workflow_document_generation_service."
                "write_po_html_and_pdf",
            ):
                invoice_result = (
                    generate_hospital_invoice_for_workflow_item(
                        item=item,
                        generated_by=self.user,
                    )
                )
                po_result = (
                    generate_factory_po_for_workflow_item(
                        item=item,
                        generated_by=self.user,
                    )
                )

                invoice_source = (
                    invoice_result[
                        "generated_document"
                    ].source_data
                )
                po_document = po_result[
                    "generated_document"
                ]
                original_po_source = (
                    po_document.source_data
                )

                self.assertEqual(
                    invoice_source[
                        "pricing_basis"
                    ][
                        "hospital_order_date"
                    ],
                    "2026-03-01",
                )
                self.assertEqual(
                    original_po_source[
                        "pricing_basis"
                    ][
                        "factory_shipping_date"
                    ],
                    "2026-07-27",
                )
                self.assertEqual(
                    len(
                        original_po_source[
                            "pricing_basis"
                        ]["rows"]
                    ),
                    2,
                )

                PricePolicy.objects.filter(
                    pk=self.late_policy.pk
                ).update(
                    factory_unit_price=(
                        Decimal("199.00")
                    )
                )

                reused = (
                    generate_factory_po_for_workflow_item(
                        item=item,
                        generated_by=self.user,
                    )
                )
                self.assertTrue(
                    reused["reused_existing"]
                )
                reused[
                    "generated_document"
                ].refresh_from_db()
                self.assertEqual(
                    reused[
                        "generated_document"
                    ].source_data,
                    original_po_source,
                )
