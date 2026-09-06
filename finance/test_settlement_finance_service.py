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
from finance.services.settlement_finance_service import (
    build_settlement_finance_dashboard_data,
)
from orders.models import Order
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


class SettlementFinanceServiceTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username=(
                    "finance-service-test"
                ),
                password="test-password",
            )
        )

        self.factory = (
            Factory.objects.create(
                name="FINANCE TEST FACTORY",
                short_name="FTF",
            )
        )

        self.order = (
            Order.objects.create(
                bon_de_commande=(
                    "FINANCE-TEST-001"
                ),
                hospital_name=(
                    "FINANCE TEST HOSPITAL"
                ),
                hospital_order_pdf=(
                    "hospital_orders/"
                    "finance-test.pdf"
                ),
                factory=self.factory,
                created_by=self.user,
            )
        )

        self.invoice = (
            GeneratedDocument
            .objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "FIN-INV-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.po = (
            GeneratedDocument
            .objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_PO
                ),
                document_number=(
                    "FIN-PO-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        self.receivable = (
            SettlementAccount
            .objects.create(
                document=self.invoice,
                direction=(
                    SettlementAccount
                    .Direction
                    .RECEIVABLE
                ),
                counterparty_name=(
                    "FINANCE TEST HOSPITAL"
                ),
                issue_date=date(
                    2026,
                    7,
                    1,
                ),
                due_date=date(
                    2026,
                    7,
                    10,
                ),
                currency="EUR",
                original_amount=(
                    Decimal("1000.00")
                ),
            )
        )

        self.payable = (
            SettlementAccount
            .objects.create(
                document=self.po,
                direction=(
                    SettlementAccount
                    .Direction
                    .PAYABLE
                ),
                counterparty_name=(
                    "FINANCE TEST FACTORY"
                ),
                issue_date=date(
                    2026,
                    7,
                    1,
                ),
                due_date=date(
                    2026,
                    8,
                    1,
                ),
                currency="EUR",
                original_amount=(
                    Decimal("400.00")
                ),
            )
        )

        PaymentTransaction.objects.create(
            account=self.receivable,
            payment_date=date(
                2026,
                7,
                5,
            ),
            amount=Decimal("250.00"),
            created_by=self.user,
        )

        PaymentTransaction.objects.create(
            account=self.payable,
            payment_date=date(
                2026,
                7,
                6,
            ),
            amount=Decimal("100.00"),
            created_by=self.user,
        )

    def test_builds_finance_totals(self):
        data = (
            build_settlement_finance_dashboard_data(
                today=date(
                    2026,
                    7,
                    15,
                )
            )
        )

        summary = data["summary"]

        self.assertEqual(
            summary["sales_total"],
            Decimal("1000.00"),
        )

        self.assertEqual(
            summary["purchase_total"],
            Decimal("400.00"),
        )

        self.assertEqual(
            summary["gross_profit"],
            Decimal("600.00"),
        )

        self.assertEqual(
            summary["receipt_total"],
            Decimal("250.00"),
        )

        self.assertEqual(
            summary["payment_total"],
            Decimal("100.00"),
        )

        self.assertEqual(
            summary["cash_net_inflow"],
            Decimal("150.00"),
        )

        self.assertEqual(
            summary[
                "receivable_remaining"
            ],
            Decimal("750.00"),
        )

        self.assertEqual(
            summary[
                "payable_remaining"
            ],
            Decimal("300.00"),
        )

    def test_builds_monthly_charts(self):
        data = (
            build_settlement_finance_dashboard_data(
                today=date(
                    2026,
                    7,
                    15,
                )
            )
        )

        accrual = (
            data["chart_data"]["accrual"]
        )

        cash = data["chart_data"]["cash"]

        self.assertEqual(
            accrual["labels"],
            ["2026-07"],
        )

        self.assertEqual(
            accrual["sales"],
            [1000.0],
        )

        self.assertEqual(
            accrual["purchases"],
            [400.0],
        )

        self.assertEqual(
            accrual["gross_profit"],
            [600.0],
        )

        self.assertEqual(
            cash["labels"],
            ["2026-07"],
        )

        self.assertEqual(
            cash["receipts"],
            [250.0],
        )

        self.assertEqual(
            cash["payments"],
            [100.0],
        )

        self.assertEqual(
            cash["net_inflow"],
            [150.0],
        )

    def test_builds_order_row(self):
        data = (
            build_settlement_finance_dashboard_data(
                today=date(
                    2026,
                    7,
                    15,
                )
            )
        )

        self.assertEqual(
            len(data["order_rows"]),
            1,
        )

        row = data["order_rows"][0]

        self.assertEqual(
            row["order_number"],
            "FINANCE-TEST-001",
        )

        self.assertEqual(
            row["invoice_number_display"],
            "FIN-INV-001",
        )

        self.assertEqual(
            row["po_number_display"],
            "FIN-PO-001",
        )

        self.assertEqual(
            row["gross_profit"],
            Decimal("600.00"),
        )

    def test_detects_overdue_receivable(
        self,
    ):
        data = (
            build_settlement_finance_dashboard_data(
                today=date(
                    2026,
                    7,
                    15,
                )
            )
        )

        self.assertEqual(
            len(
                data[
                    "overdue_receivables"
                ]
            ),
            1,
        )

        overdue = (
            data[
                "overdue_receivables"
            ][0]
        )

        self.assertEqual(
            overdue["document_number"],
            "FIN-INV-001",
        )

        self.assertEqual(
            overdue["remaining_amount"],
            Decimal("750.00"),
        )

    def test_cancelled_account_is_excluded(
        self,
    ):
        cancelled_document = (
            GeneratedDocument
            .objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "FIN-INV-CANCELLED"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        cancelled_account = (
            SettlementAccount
            .objects.create(
                document=cancelled_document,
                direction=(
                    SettlementAccount
                    .Direction
                    .RECEIVABLE
                ),
                counterparty_name=(
                    "CANCELLED HOSPITAL"
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
                    Decimal("500.00")
                ),
            )
        )

        cancelled_account.cancel(
            user=self.user,
            reason="Finance test",
        )

        data = (
            build_settlement_finance_dashboard_data(
                today=date(
                    2026,
                    7,
                    15,
                )
            )
        )

        self.assertEqual(
            data["summary"][
                "sales_total"
            ],
            Decimal("1000.00"),
        )
