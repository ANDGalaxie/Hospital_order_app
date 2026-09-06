from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from documents.models import GeneratedDocument
from factories.models import Factory
from orders.models import Order
from orders.services.admin_test_order_purge_service import (
    purge_test_order,
)
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


@override_settings(
    ALLOW_ADMIN_TEST_ORDER_PURGE=True
)
class AdminTestOrderPurgeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()

        self.superuser = (
            user_model.objects.create_superuser(
                username="purge-admin",
                email="purge@example.com",
                password="test-password",
            )
        )

        self.regular_user = (
            user_model.objects.create_user(
                username="purge-user",
                password="test-password",
            )
        )

        self.factory = Factory.objects.create(
            name="PURGE TEST FACTORY",
            short_name="PTF",
        )

        self.order = Order.objects.create(
            bon_de_commande="PURGE-TEST-001",
            hospital_name=(
                "PURGE TEST HOSPITAL"
            ),
            hospital_order_pdf=(
                "hospital_orders/purge-test.pdf"
            ),
            factory=self.factory,
            created_by=self.superuser,
        )

        self.document = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "PURGE-INVOICE-001"
                ),
                generated_by=self.superuser,
                source_data={},
            )
        )

        self.account = (
            SettlementAccount.objects.create(
                document=self.document,
                counterparty_name=(
                    "PURGE TEST HOSPITAL"
                ),
                issue_date=date(
                    2026,
                    7,
                    1,
                ),
                due_date=date(
                    2026,
                    7,
                    31,
                ),
                original_amount=(
                    Decimal("500.00")
                ),
            )
        )

        self.payment = (
            PaymentTransaction.objects.create(
                account=self.account,
                payment_date=date(
                    2026,
                    7,
                    10,
                ),
                amount=Decimal("100.00"),
                created_by=self.superuser,
            )
        )

    def test_superuser_can_purge_test_order(
        self,
    ):
        order_id = self.order.id
        document_id = self.document.id
        account_id = self.account.id
        payment_id = self.payment.id

        result = purge_test_order(
            order_id=order_id,
            requested_by=self.superuser,
        )

        self.assertEqual(
            result["order_number"],
            "PURGE-TEST-001",
        )

        self.assertFalse(
            Order.objects.filter(
                pk=order_id
            ).exists()
        )

        self.assertFalse(
            GeneratedDocument.objects.filter(
                pk=document_id
            ).exists()
        )

        self.assertFalse(
            SettlementAccount.objects.filter(
                pk=account_id
            ).exists()
        )

        self.assertFalse(
            PaymentTransaction.objects.filter(
                pk=payment_id
            ).exists()
        )

    def test_regular_user_cannot_purge(
        self,
    ):
        with self.assertRaises(
            PermissionDenied
        ):
            purge_test_order(
                order_id=self.order.id,
                requested_by=(
                    self.regular_user
                ),
            )

        self.assertTrue(
            Order.objects.filter(
                pk=self.order.id
            ).exists()
        )

        self.assertTrue(
            SettlementAccount.objects.filter(
                pk=self.account.id
            ).exists()
        )
