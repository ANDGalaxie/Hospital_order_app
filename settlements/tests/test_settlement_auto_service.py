from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from documents.models import GeneratedDocument
from factories.models import Factory
from orders.models import Order
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)
from settlements.services.settlement_auto_service import (
    ensure_settlement_account_for_document,
)


class SettlementAutoServiceTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="auto-settlement-test",
                password="test-password",
            )
        )

        self.factory = Factory.objects.create(
            name="AUTO TEST FACTORY",
            short_name="ATF",
        )

        self.order = Order.objects.create(
            bon_de_commande="AUTO-SETTLEMENT-001",
            hospital_name="AUTO TEST HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/auto-test.pdf"
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
                    "AUTO-INVOICE-001"
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
                document_number="AUTO-PO-001",
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
                                "AUTO SNAPSHOT FACTORY"
                            ),
                        },
                        "totals": {
                            "total_raw": 240,
                        },
                    },
                },
            )
        )

        self.factory_request = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_ORDER_REQUEST
                ),
                document_number=(
                    "AUTO-REQUEST-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

    def test_invoice_creates_receivable(self):
        result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
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

        self.assertEqual(
            account.issue_date,
            date(2026, 7, 1),
        )

        self.assertEqual(
            account.due_date,
            date(2026, 7, 31),
        )

    def test_po_creates_payable(self):
        result = (
            ensure_settlement_account_for_document(
                self.po
            )
        )

        self.assertEqual(
            result["status"],
            "created",
        )

        account = (
            SettlementAccount.objects.get(
                document=self.po
            )
        )

        self.assertEqual(
            account.direction,
            SettlementAccount
            .Direction
            .PAYABLE,
        )

        self.assertEqual(
            account.original_amount,
            Decimal("240.00"),
        )

        self.assertEqual(
            account.counterparty_name,
            "AUTO SNAPSHOT FACTORY",
        )

    def test_second_sync_is_unchanged(self):
        first_result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        second_result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        self.assertEqual(
            first_result["status"],
            "created",
        )

        self.assertEqual(
            second_result["status"],
            "unchanged",
        )

        self.assertEqual(
            SettlementAccount.objects
            .filter(document=self.invoice)
            .count(),
            1,
        )

    def test_account_updates_before_payment(self):
        result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        account = result["account"]

        source_data = self.invoice.source_data

        source_data[
            "invoice_data"
        ]["totals"]["total_raw"] = 550

        source_data[
            "invoice_data"
        ]["invoice"]["due_date"] = (
            "15/08/2026"
        )

        self.invoice.source_data = source_data

        self.invoice.save(
            update_fields=[
                "source_data",
            ]
        )

        result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        account.refresh_from_db()

        self.assertEqual(
            result["status"],
            "updated",
        )

        self.assertEqual(
            account.original_amount,
            Decimal("550.00"),
        )

        self.assertEqual(
            account.due_date,
            date(2026, 8, 15),
        )

        self.assertIn(
            "original_amount",
            result["updated_fields"],
        )

        self.assertIn(
            "due_date",
            result["updated_fields"],
        )

    def test_amount_change_after_payment_is_blocked(
        self,
    ):
        result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        account = result["account"]

        PaymentTransaction.objects.create(
            account=account,
            payment_date=date(2026, 7, 10),
            amount=Decimal("100.00"),
            created_by=self.user,
        )

        source_data = self.invoice.source_data

        source_data[
            "invoice_data"
        ]["totals"]["total_raw"] = 600

        self.invoice.source_data = source_data

        self.invoice.save(
            update_fields=[
                "source_data",
            ]
        )

        with self.assertRaises(
            ValidationError
        ):
            ensure_settlement_account_for_document(
                self.invoice
            )

        account.refresh_from_db()

        self.assertEqual(
            account.original_amount,
            Decimal("500.00"),
        )

    def test_cancelled_account_blocks_regeneration(
        self,
    ):
        result = (
            ensure_settlement_account_for_document(
                self.invoice
            )
        )

        account = result["account"]

        account.cancel(
            user=self.user,
            reason="测试取消",
        )

        with self.assertRaises(
            ValidationError
        ):
            ensure_settlement_account_for_document(
                self.invoice
            )

    def test_factory_request_is_unsupported(
        self,
    ):
        result = (
            ensure_settlement_account_for_document(
                self.factory_request
            )
        )

        self.assertEqual(
            result["status"],
            "unsupported",
        )

        self.assertFalse(
            SettlementAccount.objects
            .filter(
                document=self.factory_request
            )
            .exists()
        )

    def test_workflow_generation_has_auto_sync_hook(
        self,
    ):
        path = (
            Path(settings.BASE_DIR)
            / "workflow"
            / "services"
            / (
                "workflow_document_"
                "generation_service.py"
            )
        )

        self.assertTrue(path.exists())

        source = path.read_text(
            encoding="utf-8"
        )

        self.assertIn(
            (
                "ensure_settlement_"
                "account_for_document"
            ),
            source,
        )

        self.assertIn(
            "@transaction.atomic",
            source,
        )
