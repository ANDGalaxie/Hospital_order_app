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
    SettlementAccount,
)


class SettlementFinanceFilterTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="finance-filter-test",
                password="test-password",
            )
        )

        self.factory_alpha = (
            Factory.objects.create(
                name="ALPHA MEDICAL FACTORY",
                short_name="AMF",
            )
        )

        self.factory_beta = (
            Factory.objects.create(
                name="BETA MEDICAL FACTORY",
                short_name="BMF",
            )
        )

        self.order_alpha = (
            Order.objects.create(
                bon_de_commande=(
                    "FILTER-ORDER-ALPHA"
                ),
                hospital_name=(
                    "ALPHA HOSPITAL"
                ),
                hospital_order_pdf=(
                    "hospital_orders/"
                    "filter-alpha.pdf"
                ),
                factory=self.factory_alpha,
                created_by=self.user,
            )
        )

        self.order_beta = (
            Order.objects.create(
                bon_de_commande=(
                    "FILTER-ORDER-BETA"
                ),
                hospital_name=(
                    "BETA HOSPITAL"
                ),
                hospital_order_pdf=(
                    "hospital_orders/"
                    "filter-beta.pdf"
                ),
                factory=self.factory_beta,
                created_by=self.user,
            )
        )

        self.create_receivable(
            order=self.order_alpha,
            document_number=(
                "FILTER-INV-ALPHA"
            ),
            issue_date=date(
                2026,
                1,
                15,
            ),
            amount=Decimal("1000.00"),
        )

        self.create_receivable(
            order=self.order_beta,
            document_number=(
                "FILTER-INV-BETA"
            ),
            issue_date=date(
                2026,
                2,
                15,
            ),
            amount=Decimal("2000.00"),
        )

    def create_receivable(
        self,
        *,
        order,
        document_number,
        issue_date,
        amount,
    ):
        document = (
            GeneratedDocument.objects.create(
                order=order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    document_number
                ),
                generated_by=self.user,
                source_data={},
            )
        )

        return (
            SettlementAccount.objects.create(
                document=document,
                direction=(
                    SettlementAccount
                    .Direction
                    .RECEIVABLE
                ),
                counterparty_name=(
                    order.hospital_name
                ),
                issue_date=issue_date,
                due_date=date(
                    2030,
                    12,
                    31,
                ),
                currency="EUR",
                original_amount=amount,
            )
        )

    def test_service_filters_by_order(
        self,
    ):
        data = (
            build_settlement_finance_dashboard_data(
                order_query="ALPHA"
            )
        )

        self.assertEqual(
            data["summary"]["sales_total"],
            Decimal("1000.00"),
        )

        self.assertEqual(
            len(data["order_rows"]),
            1,
        )

        self.assertEqual(
            data["order_rows"][0][
                "order_number"
            ],
            "FILTER-ORDER-ALPHA",
        )

    def test_service_filters_by_date(
        self,
    ):
        data = (
            build_settlement_finance_dashboard_data(
                date_from=date(
                    2026,
                    2,
                    1,
                )
            )
        )

        self.assertEqual(
            data["summary"]["sales_total"],
            Decimal("2000.00"),
        )

        self.assertEqual(
            data["order_rows"][0][
                "order_number"
            ],
            "FILTER-ORDER-BETA",
        )

    def test_portal_filter_excludes_other_order(
        self,
    ):
        self.client.force_login(
            self.user
        )

        response = self.client.get(
            "/portal/finance/",
            {
                "order": "ALPHA",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "FILTER-ORDER-ALPHA",
        )

        self.assertNotContains(
            response,
            "FILTER-ORDER-BETA",
        )

        self.assertEqual(
            response.context[
                "filter_values"
            ]["order"],
            "ALPHA",
        )

    def test_portal_contains_chart_data(
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
            "finance-chart-data",
        )

        self.assertContains(
            response,
            "accrual-finance-chart",
        )

        self.assertContains(
            response,
            "cash-finance-chart",
        )
