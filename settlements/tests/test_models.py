from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.core.exceptions import (
    ValidationError,
)
from django.test import TestCase
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


class SettlementModelTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="settlement-test",
                password="test-password",
            )
        )

        self.factory = Factory.objects.create(
            name="SETTLEMENT TEST FACTORY",
            short_name="STF",
        )

        self.order = Order.objects.create(
            bon_de_commande=(
                "SETTLEMENT-ORDER-001"
            ),
            hospital_name=(
                "SETTLEMENT TEST HOSPITAL"
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
                    "SETTLEMENT-INVOICE-001"
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
                    "SETTLEMENT-PO-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.request_document = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_ORDER_REQUEST
                ),
                document_number=(
                    "SETTLEMENT-REQUEST-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

    def create_invoice_account(
        self,
        amount="500.00",
        issue_date=None,
        due_date=None,
    ):
        return (
            SettlementAccount.objects.create(
                document=self.invoice,
                counterparty_name=(
                    "SETTLEMENT TEST HOSPITAL"
                ),
                issue_date=(
                    issue_date
                    or timezone.localdate()
                ),
                due_date=due_date,
                original_amount=Decimal(
                    amount
                ),
            )
        )

    def test_invoice_creates_receivable(
        self,
    ):
        account = (
            self.create_invoice_account()
        )

        self.assertEqual(
            account.direction,
            SettlementAccount
            .Direction
            .RECEIVABLE,
        )

        self.assertEqual(
            account.status,
            SettlementAccount
            .Status
            .UNPAID,
        )

    def test_po_creates_payable(self):
        account = (
            SettlementAccount.objects.create(
                document=self.po,
                counterparty_name=(
                    self.factory.name
                ),
                issue_date=(
                    timezone.localdate()
                ),
                original_amount=Decimal(
                    "240.00"
                ),
            )
        )

        self.assertEqual(
            account.direction,
            SettlementAccount
            .Direction
            .PAYABLE,
        )

    def test_request_cannot_create_account(
        self,
    ):
        with self.assertRaises(
            ValidationError
        ):
            SettlementAccount.objects.create(
                document=(
                    self.request_document
                ),
                issue_date=(
                    timezone.localdate()
                ),
                original_amount=Decimal(
                    "100.00"
                ),
            )

    def test_partial_and_full_payment(
        self,
    ):
        account = (
            self.create_invoice_account()
        )

        PaymentTransaction.objects.create(
            account=account,
            payment_date=(
                timezone.localdate()
            ),
            amount=Decimal("200.00"),
            created_by=self.user,
        )

        account.refresh_from_db()

        self.assertEqual(
            account.posted_amount,
            Decimal("200.00"),
        )

        self.assertEqual(
            account.remaining_amount,
            Decimal("300.00"),
        )

        self.assertEqual(
            account.status,
            SettlementAccount
            .Status
            .PARTIALLY_PAID,
        )

        PaymentTransaction.objects.create(
            account=account,
            payment_date=(
                timezone.localdate()
            ),
            amount=Decimal("300.00"),
            created_by=self.user,
        )

        account.refresh_from_db()

        self.assertEqual(
            account.posted_amount,
            Decimal("500.00"),
        )

        self.assertEqual(
            account.remaining_amount,
            Decimal("0.00"),
        )

        self.assertEqual(
            account.status,
            SettlementAccount
            .Status
            .PAID,
        )

    def test_overpayment_is_blocked(self):
        account = (
            self.create_invoice_account()
        )

        PaymentTransaction.objects.create(
            account=account,
            payment_date=(
                timezone.localdate()
            ),
            amount=Decimal("400.00"),
            created_by=self.user,
        )

        with self.assertRaises(
            ValidationError
        ):
            PaymentTransaction.objects.create(
                account=account,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("101.00"),
                created_by=self.user,
            )

    def test_overdue_status(self):
        account = (
            self.create_invoice_account(
                issue_date=(
                    timezone.localdate()
                    - timedelta(days=30)
                ),
                due_date=(
                    timezone.localdate()
                    - timedelta(days=1)
                ),
            )
        )

        account.refresh_from_db()

        self.assertEqual(
            account.effective_status,
            SettlementAccount
            .Status
            .OVERDUE,
        )

        self.assertEqual(
            account.status,
            SettlementAccount
            .Status
            .OVERDUE,
        )

    def test_reversal_removes_payment(
        self,
    ):
        account = (
            self.create_invoice_account()
        )

        payment = (
            PaymentTransaction.objects.create(
                account=account,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("200.00"),
                created_by=self.user,
            )
        )

        payment.reverse(
            user=self.user,
            reason="测试冲销",
        )

        account.refresh_from_db()
        payment.refresh_from_db()

        self.assertEqual(
            payment.status,
            PaymentTransaction
            .Status
            .REVERSED,
        )

        self.assertEqual(
            account.posted_amount,
            Decimal("0.00"),
        )

        self.assertEqual(
            account.status,
            SettlementAccount
            .Status
            .UNPAID,
        )

    def test_direct_delete_is_blocked(
        self,
    ):
        account = (
            self.create_invoice_account()
        )

        payment = (
            PaymentTransaction.objects.create(
                account=account,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("100.00"),
                created_by=self.user,
            )
        )

        with self.assertRaises(
            ValidationError
        ):
            payment.delete()

        with self.assertRaises(
            ValidationError
        ):
            account.delete()
