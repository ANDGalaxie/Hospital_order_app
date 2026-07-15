from datetime import date
from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase

from documents.models import (
    GeneratedDocument,
)
from factories.models import Factory
from orders.models import Order
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


class SettlementFinancePortalTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username=(
                    "finance-portal-test"
                ),
                password="test-password",
            )
        )

        self.factory = (
            Factory.objects.create(
                name=(
                    "PORTAL FINANCE FACTORY"
                ),
                short_name="PFF",
            )
        )

        self.order = (
            Order.objects.create(
                bon_de_commande=(
                    "FINANCE-PORTAL-001"
                ),
                hospital_name=(
                    "PORTAL FINANCE HOSPITAL"
                ),
                hospital_order_pdf=(
                    "hospital_orders/"
                    "finance-portal.pdf"
                ),
                factory=self.factory,
                created_by=self.user,
            )
        )

        invoice = (
            GeneratedDocument
            .objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "FIN-INV-PORTAL"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        po = (
            GeneratedDocument
            .objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_PO
                ),
                document_number=(
                    "FIN-PO-PORTAL"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        receivable = (
            SettlementAccount
            .objects.create(
                document=invoice,
                direction=(
                    SettlementAccount
                    .Direction
                    .RECEIVABLE
                ),
                counterparty_name=(
                    "PORTAL FINANCE HOSPITAL"
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
                currency="EUR",
                original_amount=(
                    Decimal("1000.00")
                ),
            )
        )

        payable = (
            SettlementAccount
            .objects.create(
                document=po,
                direction=(
                    SettlementAccount
                    .Direction
                    .PAYABLE
                ),
                counterparty_name=(
                    "PORTAL FINANCE FACTORY"
                ),
                issue_date=date(
                    2026,
                    7,
                    1,
                ),
                due_date=date(
                    2026,
                    8,
                    31,
                ),
                currency="EUR",
                original_amount=(
                    Decimal("400.00")
                ),
            )
        )

        PaymentTransaction.objects.create(
            account=receivable,
            payment_date=date(
                2026,
                7,
                10,
            ),
            amount=Decimal("250.00"),
            created_by=self.user,
        )

        PaymentTransaction.objects.create(
            account=payable,
            payment_date=date(
                2026,
                7,
                11,
            ),
            amount=Decimal("100.00"),
            created_by=self.user,
        )

    def test_login_is_required(self):
        response = self.client.get(
            "/portal/finance/"
        )

        self.assertEqual(
            response.status_code,
            302,
        )

    def test_dashboard_renders_finance_data(
        self,
    ):
        self.client.force_login(
            self.user
        )

        response = self.client.get(
            "/portal/finance/"
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "财务实绩",
        )

        self.assertContains(
            response,
            "医院开票销售额",
        )

        self.assertContains(
            response,
            "FINANCE-PORTAL-001",
        )

        self.assertContains(
            response,
            "FIN-INV-PORTAL",
        )

        self.assertContains(
            response,
            "FIN-PO-PORTAL",
        )

        self.assertContains(
            response,
            "1 000.00 €",
        )

        self.assertContains(
            response,
            "600.00 €",
        )
