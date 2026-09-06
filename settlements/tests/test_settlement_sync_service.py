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
    SettlementAccount,
)
from settlements.services.settlement_sync_service import (
    build_settlement_account_data,
    sync_all_settlement_accounts,
    sync_document_to_settlement,
)


class SettlementSyncServiceTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="sync-test",
                password="test-password",
            )
        )

        self.factory = Factory.objects.create(
            name="SYNC TEST FACTORY",
            short_name="STF",
        )

        self.order = Order.objects.create(
            bon_de_commande=(
                "SYNC-ORDER-001"
            ),
            hospital_name=(
                "SYNC TEST HOSPITAL"
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
                    "SYNC-INVOICE-001"
                ),
                generated_by=self.user,
                source_data={
                    "invoice_data": {
                        "invoice": {
                            "invoice_date": (
                                "01/07/2026"
                            ),
                            "due_date": (
                                "31/07/2026"
                            ),
                        },
                        "items": [
                            {
                                "amount_raw": 500,
                            },
                        ],
                        "totals": {
                            "total_raw": 500,
                        },
                    },
                },
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
                    "SYNC-PO-001"
                ),
                generated_by=self.user,
                source_data={
                    "po_data": {
                        "po": {
                            "order_date_iso": (
                                "2026-07-01"
                            ),
                        },
                        "factory": {
                            "name": (
                                "SYNC SNAPSHOT FACTORY"
                            ),
                        },
                        "items": [
                            {
                                "amount_raw": 204,
                            },
                        ],
                        "totals": {
                            "total_raw": 204,
                        },
                    },
                },
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
                    "SYNC-REQUEST-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

    def test_invoice_data_is_parsed(
        self,
    ):
        data = build_settlement_account_data(
            self.invoice
        )

        self.assertEqual(
            data["original_amount"],
            Decimal("500.00"),
        )

        self.assertEqual(
            data["issue_date"],
            date(2026, 7, 1),
        )

        self.assertEqual(
            data["due_date"],
            date(2026, 7, 31),
        )

        self.assertEqual(
            data["direction"],
            SettlementAccount
            .Direction
            .RECEIVABLE,
        )

    def test_po_data_is_parsed(self):
        data = build_settlement_account_data(
            self.po
        )

        self.assertEqual(
            data["original_amount"],
            Decimal("204.00"),
        )

        self.assertEqual(
            data["issue_date"],
            date(2026, 7, 1),
        )

        self.assertIsNone(
            data["due_date"]
        )

        self.assertEqual(
            data["counterparty_name"],
            "SYNC SNAPSHOT FACTORY",
        )

    def test_invoice_account_is_created(
        self,
    ):
        result = sync_document_to_settlement(
            self.invoice
        )

        self.assertEqual(
            result["status"],
            "created",
        )

        account = (
            SettlementAccount.objects.get(
                document=self.invoice
            )
        )

        self.assertEqual(
            account.direction,
            SettlementAccount
            .Direction
            .RECEIVABLE,
        )

        self.assertEqual(
            account.original_amount,
            Decimal("500.00"),
        )

    def test_sync_is_idempotent(self):
        first = sync_document_to_settlement(
            self.invoice
        )

        second = sync_document_to_settlement(
            self.invoice
        )

        self.assertEqual(
            first["status"],
            "created",
        )

        self.assertEqual(
            second["status"],
            "existing",
        )

        self.assertEqual(
            SettlementAccount.objects
            .filter(document=self.invoice)
            .count(),
            1,
        )

    def test_dry_run_does_not_create(
        self,
    ):
        result = sync_document_to_settlement(
            self.invoice,
            dry_run=True,
        )

        self.assertEqual(
            result["status"],
            "would_create",
        )

        self.assertFalse(
            SettlementAccount.objects
            .filter(document=self.invoice)
            .exists()
        )

    def test_request_is_unsupported(
        self,
    ):
        result = sync_document_to_settlement(
            self.request_document
        )

        self.assertEqual(
            result["status"],
            "unsupported",
        )

        self.assertFalse(
            SettlementAccount.objects
            .filter(
                document=(
                    self.request_document
                )
            )
            .exists()
        )

    def test_sync_all_creates_invoice_and_po(
        self,
    ):
        report = (
            sync_all_settlement_accounts()
        )

        self.assertEqual(
            report["scanned"],
            2,
        )

        self.assertEqual(
            report["created"],
            2,
        )

        self.assertEqual(
            report["error"],
            0,
        )

        self.assertEqual(
            SettlementAccount.objects.count(),
            2,
        )

    def test_missing_amount_is_reported(
        self,
    ):
        document = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "SYNC-INVOICE-NO-AMOUNT"
                ),
                generated_by=self.user,
                source_data={
                    "invoice_data": {
                        "invoice": {
                            "invoice_date": (
                                "01/07/2026"
                            ),
                        },
                        "items": [],
                    },
                },
            )
        )

        result = sync_document_to_settlement(
            document
        )

        self.assertEqual(
            result["status"],
            "error",
        )

        self.assertIn(
            "金额",
            result["message"],
        )
