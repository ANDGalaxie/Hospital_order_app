from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    GeneratedDocument,
)
from factories.models import Factory
from orders.models import Order
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


class SettlementPortalTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_superuser(
                username="settlement-portal",
                email="settlement@example.com",
                password="test-password",
            )
        )

        self.client.force_login(self.user)

        self.factory = Factory.objects.create(
            name="PORTAL SETTLEMENT FACTORY",
            short_name="PSF",
        )

        self.order = Order.objects.create(
            bon_de_commande="PORTAL-SETTLE-001",
            hospital_name=(
                "PORTAL SETTLEMENT HOSPITAL"
            ),
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            created_by=self.user,
        )

        self.invoice = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "PORTAL-INVOICE-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.po = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_PO
                ),
                document_number=(
                    "PORTAL-PO-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.receivable = (
            SettlementAccount.objects.create(
                document=self.invoice,
                counterparty_name=(
                    "PORTAL SETTLEMENT HOSPITAL"
                ),
                issue_date=(
                    timezone.localdate()
                    - timedelta(days=10)
                ),
                due_date=(
                    timezone.localdate()
                    + timedelta(days=20)
                ),
                original_amount=Decimal(
                    "500.00"
                ),
            )
        )

        self.payable = (
            SettlementAccount.objects.create(
                document=self.po,
                counterparty_name=(
                    "PORTAL SETTLEMENT FACTORY"
                ),
                issue_date=(
                    timezone.localdate()
                    - timedelta(days=5)
                ),
                original_amount=Decimal(
                    "240.00"
                ),
            )
        )

        self.receipt = (
            PaymentTransaction.objects.create(
                account=self.receivable,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("200.00"),
                method=(
                    PaymentTransaction
                    .Method
                    .BANK_TRANSFER
                ),
                reference="BANK-RECEIPT-001",
                created_by=self.user,
            )
        )

        self.payment = (
            PaymentTransaction.objects.create(
                account=self.payable,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("100.00"),
                method=(
                    PaymentTransaction
                    .Method
                    .BANK_TRANSFER
                ),
                reference="BANK-PAYMENT-001",
                created_by=self.user,
            )
        )

    def test_settlement_home(self):
        response = self.client.get(
            reverse(
                "portal:settlement_home"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "医院应收",
        )

        self.assertContains(
            response,
            "工厂应付",
        )

        self.assertContains(
            response,
            "收付款流水",
        )

        self.assertContains(
            response,
            "300.00",
        )

        self.assertContains(
            response,
            "140.00",
        )

    def test_receivable_list_only_shows_invoice(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:settlement_receivables"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.invoice.document_number,
        )

        self.assertNotContains(
            response,
            self.po.document_number,
        )

        self.assertContains(
            response,
            "300.00",
        )

    def test_payable_list_only_shows_po(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:settlement_payables"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.po.document_number,
        )

        self.assertNotContains(
            response,
            self.invoice.document_number,
        )

        self.assertContains(
            response,
            "140.00",
        )

    def test_transaction_list(self):
        response = self.client.get(
            reverse(
                "portal:settlement_transactions"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "BANK-RECEIPT-001",
        )

        self.assertContains(
            response,
            "BANK-PAYMENT-001",
        )

        self.assertContains(
            response,
            "收款",
        )

        self.assertContains(
            response,
            "付款",
        )

    def test_transaction_direction_filter(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:settlement_transactions"
            ),
            {
                "direction": (
                    SettlementAccount
                    .Direction
                    .RECEIVABLE
                ),
            },
        )

        self.assertContains(
            response,
            "BANK-RECEIPT-001",
        )

        self.assertNotContains(
            response,
            "BANK-PAYMENT-001",
        )
