from datetime import date
from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase
from django.urls import reverse

from factories.models import Factory
from portal.forms.price_policy_forms import (
    PricePolicyPortalForm,
)
from pricing.models import PricePolicy
from products.models import (
    Product,
    ProductCategory,
)


class PricePolicyPortalTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_superuser(
                username="price-admin",
                email="price@example.com",
                password="test-password",
            )
        )

        self.client.force_login(self.user)

        self.factory = Factory.objects.create(
            name="PORTAL TEST FACTORY",
            short_name="PTF",
        )

        self.department = (
            ProductCategory.objects.create(
                name="PORTAL TEST DEPARTMENT",
                node_type=(
                    ProductCategory
                    .NodeType
                    .DEPARTMENT
                ),
            )
        )

        self.factory_node = (
            ProductCategory.objects.create(
                name="PORTAL TEST FACTORY NODE",
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
                name="PORTAL TEST CATEGORY",
                parent=self.factory_node,
                node_type=(
                    ProductCategory
                    .NodeType
                    .CATEGORY
                ),
            )
        )

        self.product = Product.objects.create(
            code="PORTAL-PRICE-001",
            description="Portal price test",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal(
                "1.00"
            ),
            factory_unit_price=Decimal(
                "1.00"
            ),
        )

    def create_policy(self):
        policy = PricePolicy(
            name="Portal test policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            end_date=None,
            hospital_unit_price=Decimal(
                "250.00"
            ),
            factory_unit_price=Decimal(
                "120.00"
            ),
            expiration_threshold_days=365,
            expiration_discount_rate=Decimal(
                "0.30"
            ),
            is_active=True,
        )

        policy.full_clean()
        policy.save()

        return policy

    def test_form_converts_percent_to_rate(self):
        form = PricePolicyPortalForm(
            data={
                "name": "Form conversion test",
                "factory": self.factory.id,
                "category": self.category.id,
                "start_date": "2027-01-01",
                "end_date": "",
                "hospital_unit_price": "260.00",
                "factory_unit_price": "125.00",
                "expiration_threshold_days": "365",
                "expiration_discount_percent": "30",
                "notes": "",
                "is_active": "on",
            }
        )

        self.assertTrue(
            form.is_valid(),
            form.errors,
        )

        policy = form.save()

        self.assertEqual(
            policy.expiration_discount_rate,
            Decimal("0.30"),
        )

    def test_list_and_detail_pages(self):
        policy = self.create_policy()

        list_response = self.client.get(
            reverse(
                "portal:library_prices"
            )
        )

        detail_response = self.client.get(
            reverse(
                "portal:library_price_policy_detail",
                args=[policy.id],
            )
        )

        self.assertEqual(
            list_response.status_code,
            200,
        )

        self.assertContains(
            list_response,
            "Portal test policy",
        )

        self.assertEqual(
            detail_response.status_code,
            200,
        )

        self.assertContains(
            detail_response,
            "250.00",
        )

    def test_simulator_resolves_policy(self):
        policy = self.create_policy()

        response = self.client.get(
            reverse(
                "portal:library_price_policy_simulator"
            ),
            {
                "product": self.product.id,
                "target_date": "2026-07-01",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            policy.name,
        )

        self.assertContains(
            response,
            "匹配成功",
        )
