from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from factories.models import Factory
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from pricing.services.price_policy_service import (
    apply_price_policy_to_order,
    calculate_expiration_pricing,
    resolve_price_policy_for_product,
)
from products.models import Product, ProductCategory


class PricePolicyEngineTests(TestCase):
    def setUp(self):
        self.factory = Factory.objects.create(
            name="TEST FACTORY A",
            short_name="TFA",
        )

        self.department = ProductCategory.objects.create(
            name="TEST DEPARTMENT",
            node_type=(
                ProductCategory.NodeType.DEPARTMENT
            ),
        )

        self.factory_node = (
            ProductCategory.objects.create(
                name="TEST FACTORY NODE",
                parent=self.department,
                node_type=(
                    ProductCategory.NodeType.FACTORY
                ),
                factory=self.factory,
            )
        )

        self.category = ProductCategory.objects.create(
            name="TEST CATEGORY",
            parent=self.factory_node,
            node_type=(
                ProductCategory.NodeType.CATEGORY
            ),
        )

        self.product = Product.objects.create(
            code="TEST-PRODUCT-001",
            description="Test product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("1.00"),
            factory_unit_price=Decimal("1.00"),
        )

    def create_policy(
        self,
        *,
        name,
        factory=None,
        category=None,
        start_date=None,
        end_date=None,
        hospital_price="250.00",
        factory_price="120.00",
        discount="0.30",
        threshold_days=365,
    ):
        policy = PricePolicy(
            name=name,
            factory=factory,
            category=category,
            start_date=start_date,
            end_date=end_date,
            hospital_unit_price=Decimal(
                hospital_price
            ),
            factory_unit_price=Decimal(
                factory_price
            ),
            expiration_discount_rate=Decimal(
                discount
            ),
            expiration_threshold_days=(
                threshold_days
            ),
            is_active=True,
        )

        policy.full_clean()
        policy.save()

        return policy

    def test_specific_category_rule_beats_other_rules(self):
        global_policy = self.create_policy(
            name="Global",
            hospital_price="200.00",
            factory_price="100.00",
        )

        factory_policy = self.create_policy(
            name="Factory",
            factory=self.factory,
            hospital_price="220.00",
            factory_price="110.00",
        )

        category_policy = self.create_policy(
            name="Category",
            factory=self.factory,
            category=self.category,
            hospital_price="250.00",
            factory_price="120.00",
        )

        resolved = resolve_price_policy_for_product(
            product=self.product,
            target_date=date(2026, 7, 1),
        )

        self.assertEqual(
            resolved["policy"],
            category_policy,
        )
        self.assertEqual(
            resolved["scope"],
            "factory_category",
        )

        self.assertNotEqual(
            resolved["policy"],
            factory_policy,
        )
        self.assertNotEqual(
            resolved["policy"],
            global_policy,
        )

    def test_global_policy_is_valid_fallback(self):
        global_policy = self.create_policy(
            name="Global fallback",
        )

        resolved = resolve_price_policy_for_product(
            product=self.product,
            target_date=date(2026, 7, 1),
        )

        self.assertEqual(
            resolved["policy"],
            global_policy,
        )
        self.assertEqual(
            resolved["scope"],
            "global",
        )

    def test_historical_date_ranges(self):
        old_policy = self.create_policy(
            name="Before 2026-04-20",
            end_date=date(2026, 4, 19),
            hospital_price="300.00",
            factory_price="165.00",
            discount="0.20",
        )

        middle_policy = self.create_policy(
            name="2026-04-20 to 2026-05-31",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 5, 31),
            hospital_price="270.00",
            factory_price="120.00",
            discount="0.30",
        )

        current_policy = self.create_policy(
            name="From 2026-06-01",
            start_date=date(2026, 6, 1),
            hospital_price="250.00",
            factory_price="120.00",
            discount="0.30",
        )

        cases = [
            (
                date(2026, 4, 19),
                old_policy,
            ),
            (
                date(2026, 4, 20),
                middle_policy,
            ),
            (
                date(2026, 5, 31),
                middle_policy,
            ),
            (
                date(2026, 6, 1),
                current_policy,
            ),
        ]

        for target_date, expected in cases:
            resolved = (
                resolve_price_policy_for_product(
                    product=self.product,
                    target_date=target_date,
                )
            )

            self.assertEqual(
                resolved["policy"],
                expected,
            )

    def test_overlapping_same_scope_is_rejected(self):
        self.create_policy(
            name="First",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 6, 30),
        )

        overlapping = PricePolicy(
            name="Overlapping",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 12, 31),
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

        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_conflicting_top_scope_is_blocking(self):
        first = PricePolicy.objects.create(
            name="Conflicting first",
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
        second = PricePolicy.objects.create(
            name="Conflicting second",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal(
                "260.00"
            ),
            factory_unit_price=Decimal(
                "130.00"
            ),
            expiration_discount_rate=Decimal(
                "0.30"
            ),
            expiration_threshold_days=365,
            is_active=True,
        )

        resolved = resolve_price_policy_for_product(
            product=self.product,
            target_date=date(2026, 3, 1),
        )

        self.assertIsNone(
            resolved["policy"]
        )
        self.assertTrue(
            resolved["is_ambiguous"]
        )
        self.assertIn(
            str(first.id),
            resolved["errors"][0],
        )
        self.assertIn(
            str(second.id),
            resolved["errors"][0],
        )

    def test_category_must_belong_to_factory(self):
        other_factory = Factory.objects.create(
            name="TEST FACTORY B",
            short_name="TFB",
        )

        invalid_policy = PricePolicy(
            name="Wrong factory",
            factory=other_factory,
            category=self.category,
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
        )

        with self.assertRaises(ValidationError):
            invalid_policy.full_clean()

    def test_expiration_boundary_is_strict(self):
        reference_date = date(2026, 7, 1)

        exact_boundary = calculate_expiration_pricing(
            factory_unit_price=Decimal("120.00"),
            expiration_date=date(2027, 7, 1),
            reference_date=reference_date,
            expiration_threshold_days=365,
            expiration_discount_rate=Decimal(
                "0.30"
            ),
        )

        one_day_before = calculate_expiration_pricing(
            factory_unit_price=Decimal("120.00"),
            expiration_date=date(2027, 6, 30),
            reference_date=reference_date,
            expiration_threshold_days=365,
            expiration_discount_rate=Decimal(
                "0.30"
            ),
        )

        self.assertFalse(
            exact_boundary["discount_applied"]
        )
        self.assertEqual(
            exact_boundary["final_price"],
            Decimal("120.00"),
        )

        self.assertTrue(
            one_day_before["discount_applied"]
        )
        self.assertEqual(
            one_day_before["final_price"],
            Decimal("84.00"),
        )

    def test_apply_policy_freezes_only_hospital_snapshot(self):
        policy = self.create_policy(
            name="Snapshot policy",
            factory=self.factory,
            category=self.category,
            hospital_price="250.00",
            factory_price="120.00",
            discount="0.30",
            threshold_days=365,
        )

        user = get_user_model().objects.create_user(
            username="price-test-user",
            password="test",
        )

        order = Order.objects.create(
            bon_de_commande="PRICE-TEST-001",
            hospital_name="TEST HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            order_date=date(2026, 6, 15),
            extracted_order_data={
                "header": {
                    "order_date": "2026-06-15",
                }
            },
            created_by=user,
        )

        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=1,
        )

        result = apply_price_policy_to_order(
            order
        )

        item.refresh_from_db()

        self.assertEqual(
            result["updated_count"],
            1,
        )
        self.assertEqual(
            item.price_policy,
            policy,
        )
        self.assertEqual(
            item.hospital_unit_price,
            Decimal("250.00"),
        )
        self.assertIsNone(
            item.factory_unit_price,
        )
        self.assertIsNone(
            item.expiration_discount_rate,
        )
        self.assertIsNone(
            item.expiration_threshold_days,
        )
        self.assertEqual(
            item.price_policy_date,
            date(2026, 6, 15),
        )
