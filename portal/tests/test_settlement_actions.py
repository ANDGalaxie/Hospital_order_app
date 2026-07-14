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


class SettlementActionTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_superuser(
                username="settlement-actions",
                email="actions@example.com",
                password="test-password",
            )
        )

        self.client.force_login(self.user)

        self.factory = Factory.objects.create(
            name="ACTION TEST FACTORY",
            short_name="ATF",
        )

        self.order = Order.objects.create(
            bon_de_commande=(
                "SETTLEMENT-ACTION-001"
            ),
            hospital_name=(
                "ACTION TEST HOSPITAL"
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
                    "ACTION-INVOICE-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.account = (
            SettlementAccount.objects.create(
                document=self.invoice,
                counterparty_name=(
                    "ACTION TEST HOSPITAL"
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

    def detail_url(self):
        return reverse(
            "portal:settlement_account_detail",
            args=[self.account.id],
        )

    def payment_url(self):
        return reverse(
            "portal:settlement_record_payment",
            args=[self.account.id],
        )

    def due_date_url(self):
        return reverse(
            "portal:settlement_update_due_date",
            args=[self.account.id],
        )

    def test_account_detail_page(self):
        response = self.client.get(
            self.detail_url()
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.invoice.document_number,
        )

        self.assertContains(
            response,
            "登记收款",
        )

        self.assertContains(
            response,
            "500.00",
        )

    def test_record_payment(self):
        response = self.client.post(
            self.payment_url(),
            {
                "payment_date": (
                    timezone.localdate()
                    .isoformat()
                ),
                "amount": "200.00",
                "method": (
                    PaymentTransaction
                    .Method
                    .BANK_TRANSFER
                ),
                "reference": (
                    "ACTION-BANK-001"
                ),
                "notes": "First receipt",
            },
        )

        self.assertEqual(
            response.status_code,
            302,
        )

        payment = (
            PaymentTransaction.objects.get(
                account=self.account
            )
        )

        self.assertEqual(
            payment.amount,
            Decimal("200.00"),
        )

        self.assertEqual(
            payment.reference,
            "ACTION-BANK-001",
        )

        self.account.refresh_from_db()

        self.assertEqual(
            self.account.status,
            SettlementAccount
            .Status
            .PARTIALLY_PAID,
        )

        self.assertEqual(
            self.account.remaining_amount,
            Decimal("300.00"),
        )

    def test_overpayment_is_rejected(self):
        PaymentTransaction.objects.create(
            account=self.account,
            payment_date=(
                timezone.localdate()
            ),
            amount=Decimal("100.00"),
            created_by=self.user,
        )

        response = self.client.post(
            self.payment_url(),
            {
                "payment_date": (
                    timezone.localdate()
                    .isoformat()
                ),
                "amount": "450.00",
                "method": (
                    PaymentTransaction
                    .Method
                    .BANK_TRANSFER
                ),
                "reference": "OVERPAYMENT",
                "notes": "",
            },
        )

        self.assertEqual(
            response.status_code,
            400,
        )

        self.assertEqual(
            PaymentTransaction.objects
            .filter(account=self.account)
            .count(),
            1,
        )

        self.assertContains(
            response,
            "不能超过当前剩余金额",
            status_code=400,
        )

    def test_update_due_date(self):
        new_due_date = (
            timezone.localdate()
            + timedelta(days=45)
        )

        response = self.client.post(
            self.due_date_url(),
            {
                "due_date": (
                    new_due_date.isoformat()
                ),
            },
        )

        self.assertEqual(
            response.status_code,
            302,
        )

        self.account.refresh_from_db()

        self.assertEqual(
            self.account.due_date,
            new_due_date,
        )

    def test_due_date_before_issue_date_rejected(
        self,
    ):
        invalid_due_date = (
            self.account.issue_date
            - timedelta(days=1)
        )

        response = self.client.post(
            self.due_date_url(),
            {
                "due_date": (
                    invalid_due_date.isoformat()
                ),
            },
        )

        self.assertEqual(
            response.status_code,
            400,
        )

        self.assertContains(
            response,
            "不能早于开立日期",
            status_code=400,
        )

    def test_reverse_payment(self):
        payment = (
            PaymentTransaction.objects.create(
                account=self.account,
                payment_date=(
                    timezone.localdate()
                ),
                amount=Decimal("200.00"),
                reference="TO-REVERSE",
                created_by=self.user,
            )
        )

        response = self.client.post(
            reverse(
                "portal:"
                "settlement_reverse_transaction",
                args=[payment.id],
            ),
            {
                "reversal_reason": (
                    "银行流水登记错误"
                ),
            },
        )

        self.assertEqual(
            response.status_code,
            302,
        )

        payment.refresh_from_db()
        self.account.refresh_from_db()

        self.assertEqual(
            payment.status,
            PaymentTransaction
            .Status
            .REVERSED,
        )

        self.assertEqual(
            payment.reversal_reason,
            "银行流水登记错误",
        )

        self.assertEqual(
            self.account.posted_amount,
            Decimal("0.00"),
        )

        self.assertEqual(
            self.account.status,
            SettlementAccount
            .Status
            .UNPAID,
        )

    def test_payment_action_requires_post(self):
        response = self.client.get(
            self.payment_url()
        )

        self.assertEqual(
            response.status_code,
            405,
        )

    def test_due_date_action_requires_post(self):
        response = self.client.get(
            self.due_date_url()
        )

        self.assertEqual(
            response.status_code,
            405,
        )
