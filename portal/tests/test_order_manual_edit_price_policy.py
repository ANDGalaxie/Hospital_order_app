from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from documents.models import GeneratedDocument
from factories.models import Factory
from orders.models import Order, OrderItem
from portal.services.order_portal_service import (
    save_order_manual_edit,
)
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class OrderManualEditPricePolicyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="manual-price-test",
            password="test",
        )
        self.factory = Factory.objects.create(
            name="MANUAL PRICE FACTORY",
            short_name="MPF",
        )
        self.department = ProductCategory.objects.create(
            name="MANUAL PRICE DEPARTMENT",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        self.factory_node = ProductCategory.objects.create(
            name="MANUAL PRICE FACTORY NODE",
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        self.category = ProductCategory.objects.create(
            name="MANUAL PRICE CATEGORY",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.unpriced_category = ProductCategory.objects.create(
            name="MANUAL UNPRICED CATEGORY",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product_a = Product.objects.create(
            code="MANUAL-A",
            description="Original product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("10.00"),
            factory_unit_price=Decimal("5.00"),
        )
        self.product_b = Product.objects.create(
            code="BMA-2.5040",
            description="Corrected product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("20.00"),
            factory_unit_price=Decimal("8.00"),
        )
        self.product_without_policy = Product.objects.create(
            code="NO-POLICY",
            description="No policy product",
            category=self.unpriced_category,
            factory=self.factory,
            hospital_unit_price=Decimal("30.00"),
            factory_unit_price=None,
        )
        self.policy = PricePolicy.objects.create(
            name="Manual edit policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.order = Order.objects.create(
            bon_de_commande="MANUAL-PRICE-001",
            hospital_name="TEST HOSPITAL",
            hospital_order_pdf="hospital_orders/test.pdf",
            factory=self.factory,
            order_date=date(2026, 6, 15),
            created_by=self.user,
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            product=self.product_a,
            product_code=self.product_a.code,
            description=self.product_a.description,
            requested_quantity=2,
            hospital_unit_price=Decimal("250.00"),
            factory_unit_price=Decimal("120.00"),
            expiration_discount_rate=Decimal("0.30"),
            expiration_threshold_days=365,
            price_policy=self.policy,
            price_policy_date=self.order.order_date,
        )

    def edit_product(self, product):
        prefix = f"item_{self.item.id}_"
        return save_order_manual_edit(
            self.order,
            {
                "bon_de_commande": self.order.bon_de_commande,
                "hospital_name": self.order.hospital_name,
                "notes": "",
                "shipping_address_text": "Shipping address",
                "billing_address_text": "Billing address",
                prefix + "product_code": product.code,
                prefix + "description": product.description,
                prefix + "requested_quantity": "2",
                prefix + "hospital_unit_price": "999.00",
            },
        )

    def test_product_change_automatically_reapplies_hospital_policy(self):
        _errors, warnings = self.edit_product(self.product_b)

        self.item.refresh_from_db()
        self.assertEqual(self.item.product, self.product_b)
        self.assertEqual(self.item.price_policy, self.policy)
        self.assertEqual(
            self.item.price_policy_date,
            self.order.order_date,
        )
        self.assertEqual(
            self.item.hospital_unit_price,
            Decimal("250.00"),
        )
        warning_text = " ".join(warnings)
        self.assertNotIn("缺少有效工厂采购价", warning_text)
        self.assertNotIn("尚未应用医院价格规则", warning_text)

    def test_basic_validation_does_not_require_factory_price(self):
        self.product_b.factory_unit_price = None
        self.product_b.save(update_fields=["factory_unit_price"])

        _errors, warnings = self.edit_product(self.product_b)

        self.item.refresh_from_db()
        self.assertIsNone(self.item.factory_unit_price)
        self.assertEqual(self.item.price_policy, self.policy)
        self.assertNotIn(
            "缺少有效工厂采购价",
            " ".join(warnings),
        )

    def test_missing_policy_keeps_product_edit_and_returns_warning(self):
        _errors, warnings = self.edit_product(
            self.product_without_policy
        )

        self.item.refresh_from_db()
        self.assertEqual(
            self.item.product,
            self.product_without_policy,
        )
        self.assertIsNone(self.item.price_policy_id)
        self.assertIsNone(self.item.price_policy_date)
        self.assertIn(
            "尚未应用医院价格规则",
            " ".join(warnings),
        )

    def test_generated_documents_prevent_automatic_repricing(self):
        batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=1,
            batch_date=date(2026, 7, 1),
            month_key="2026-07",
        )
        invoice = GeneratedDocument.objects.create(
            order=self.order,
            shipment_batch=batch,
            document_type=(
                GeneratedDocument.DocumentType.HOSPITAL_INVOICE
            ),
            document_number="INV-MANUAL-PRICE-001",
            generated_by=self.user,
        )
        DocumentWorkflowItem.objects.create(
            order=self.order,
            shipment_batch=batch,
            invoice_document=invoice,
        )

        _errors, warnings = self.edit_product(self.product_b)

        self.item.refresh_from_db()
        self.assertEqual(self.item.product, self.product_b)
        self.assertIsNone(self.item.price_policy_id)
        self.assertIsNone(self.item.price_policy_date)
        self.assertEqual(
            self.item.hospital_unit_price,
            self.product_b.hospital_unit_price,
        )
        warning_text = " ".join(warnings)
        self.assertIn("关联的 Invoice 或 PO", warning_text)
        self.assertIn("尚未应用医院价格规则", warning_text)
