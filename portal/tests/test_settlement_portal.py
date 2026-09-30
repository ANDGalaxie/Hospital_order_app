from copy import deepcopy
from datetime import date, timedelta
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
from portal.services.settlement_portal_service import (
    get_payable_expected_arrival,
)
from shipments.models import ShipmentBatch
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


    def create_sort_payable(
        self, arrival=None, *, issue_date=date(2026, 7, 1),
        amount="100.00", batch_number=None,
    ):
        number = GeneratedDocument.objects.count() + 1
        batch = None
        if batch_number is not None:
            batch = ShipmentBatch.objects.create(
                order=self.order,
                batch_number=batch_number,
                month_key="2026-07",
            )
        document = GeneratedDocument.objects.create(
            order=self.order,
            shipment_batch=batch,
            document_type=GeneratedDocument.DocumentType.FACTORY_PO,
            document_number=f"SORT-PO-{number:03}",
            generated_by=self.user,
            source_data={"po_data": {"po": {"expected_arrival_iso": arrival}}},
        )
        return SettlementAccount.objects.create(
            document=document,
            issue_date=issue_date,
            original_amount=Decimal(amount),
        )

    def get_sorted_payables(self, **params):
        response = self.client.get(
            reverse("portal:settlement_payables"),
            {"q": "SORT-PO-", **params},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def assert_account_order(self, response, accounts):
        self.assertEqual(
            [account.id for account in response.context["page_obj"]],
            [account.id for account in accounts],
        )

    def test_payable_default_orders_batches_by_saved_arrival(self):
        # Create Batch 2 first so the old descending ID order would fail.
        batch2 = self.create_sort_payable("2026-07-10", batch_number=2)
        batch1 = self.create_sort_payable("2026-07-20", batch_number=1)

        response = self.get_sorted_payables()

        self.assert_account_order(response, [batch2, batch1])
        self.assertEqual(response.context["sort"], "payment_priority")
        self.assertContains(response, "Batch 2")
        self.assertContains(response, "Batch 1")
        self.assertContains(response, "2026-07-10")
        self.assertContains(response, "<th>预计到货</th>", html=True)
        self.assertContains(response, 'name="sort"')
        self.assertNotContains(response, 'name="due"')
        self.assertNotContains(response, "<th>截止日期</th>", html=True)

    def test_payable_missing_arrival_follows_dated_unpaid_accounts(self):
        early = self.create_sort_payable("2026-07-10")
        late = self.create_sort_payable("2026-07-20")
        missing = self.create_sort_payable()

        self.assert_account_order(
            self.get_sorted_payables(), [early, late, missing],
        )

    def test_payable_unpaid_precedes_paid_and_cancelled(self):
        paid = self.create_sort_payable("2026-07-01")
        PaymentTransaction.objects.create(
            account=paid, amount=paid.original_amount, created_by=self.user,
        )
        cancelled = self.create_sort_payable("2026-07-02")
        cancelled.cancel(user=self.user, reason="Test cancellation")
        unpaid = self.create_sort_payable("2026-07-20")
        missing = self.create_sort_payable()

        self.assert_account_order(
            self.get_sorted_payables(), [unpaid, missing, paid, cancelled],
        )

    def test_payable_generated_desc_uses_document_time(self):
        newer = self.create_sort_payable("2026-07-20")
        older = self.create_sort_payable("2026-07-01")
        GeneratedDocument.objects.filter(pk=older.document_id).update(
            generated_at=timezone.now() - timedelta(days=2),
        )

        self.assert_account_order(
            self.get_sorted_payables(sort="generated_desc"), [newer, older],
        )

    def test_payable_issue_desc_preserves_original_order(self):
        newer = self.create_sort_payable(
            "2026-07-20", issue_date=date(2026, 7, 3),
        )
        older = self.create_sort_payable(
            "2026-07-01", issue_date=date(2026, 7, 1),
        )
        same_issue = self.create_sort_payable(
            "2026-07-30", issue_date=date(2026, 7, 3),
        )

        self.assert_account_order(
            self.get_sorted_payables(sort="issue_desc"),
            [same_issue, newer, older],
        )

    def test_payable_arrival_asc_includes_paid_and_missing_last(self):
        paid = self.create_sort_payable("2026-07-01")
        PaymentTransaction.objects.create(
            account=paid, amount=paid.original_amount, created_by=self.user,
        )
        late = self.create_sort_payable("2026-07-20")
        missing = self.create_sort_payable()

        self.assert_account_order(
            self.get_sorted_payables(sort="arrival_asc"), [paid, late, missing],
        )

    def test_payable_remaining_desc_uses_posted_balance_and_arrival(self):
        partial = self.create_sort_payable("2026-07-01", amount="300.00")
        PaymentTransaction.objects.create(
            account=partial, amount=Decimal("250.00"), created_by=self.user,
        )
        early = self.create_sort_payable("2026-07-10")
        late = self.create_sort_payable("2026-07-20")
        missing = self.create_sort_payable()
        same_arrival = self.create_sort_payable("2026-07-10")
        # remaining_desc uses ID, not generation time, for equal balances/dates.
        GeneratedDocument.objects.filter(pk=same_arrival.document_id).update(
            generated_at=timezone.now() - timedelta(days=2),
        )

        self.assert_account_order(
            self.get_sorted_payables(sort="remaining_desc"),
            [same_arrival, early, late, missing, partial],
        )

    def test_payable_arrival_ties_use_generation_time_then_id(self):
        newer = self.create_sort_payable("2026-07-10")
        older = self.create_sort_payable("2026-07-10")
        tie = self.create_sort_payable("2026-07-10")
        instant = timezone.now()
        GeneratedDocument.objects.filter(
            pk__in=[newer.document_id, tie.document_id],
        ).update(generated_at=instant)
        GeneratedDocument.objects.filter(pk=older.document_id).update(
            generated_at=instant - timedelta(days=1),
        )

        for sort in ("payment_priority", "arrival_asc", "generated_desc"):
            with self.subTest(sort=sort):
                self.assert_account_order(
                    self.get_sorted_payables(sort=sort), [tie, newer, older],
                )

    def test_payable_invalid_sort_falls_back_to_payment_priority(self):
        early = self.create_sort_payable("2026-07-10")
        late = self.create_sort_payable("2026-07-20")

        response = self.get_sorted_payables(sort="unknown")

        self.assertEqual(response.context["sort"], "payment_priority")
        self.assert_account_order(response, [early, late])

    def test_payable_sorts_before_pagination_and_preserves_query(self):
        accounts = [
            self.create_sort_payable(date(2026, 7, day).isoformat())
            for day in range(1, 27)
        ]
        response = self.get_sorted_payables(
            sort="arrival_asc", status="unpaid", page=1,
        )
        self.assert_account_order(response, accounts[:25])
        self.assertEqual(response.context["summary"]["count"], 26)
        self.assertContains(
            response,
            '<a href="?q=SORT-PO-&amp;sort=arrival_asc&amp;status=unpaid&amp;page=2">'
            '下一页</a>',
            html=True,
        )
        self.assert_account_order(
            self.get_sorted_payables(
                sort="arrival_asc", status="unpaid", page=2,
            ),
            accounts[25:],
        )

    def test_payable_missing_or_invalid_arrival_renders_dash(self):
        for payload in (
            None, {}, [], {"po_data": None}, {"po_data": {"po": []}},
            {"po_data": {"po": {"expected_arrival_iso": "invalid"}}},
        ):
            with self.subTest(payload=payload):
                GeneratedDocument.objects.filter(pk=self.po.pk).update(
                    source_data=payload,
                )
                response = self.client.get(
                    reverse("portal:settlement_payables"),
                )
                self.assertEqual(response.status_code, 200)
                account = response.context["page_obj"][0]
                self.assertIsNone(account.portal_expected_arrival)
                self.assertEqual(
                    account.portal_generated_at, account.document.generated_at,
                )
                self.assertContains(response, "<td>—</td>", html=True)

    def test_saved_arrival_supports_existing_payload_shapes_without_writes(self):
        for payload in (
            {"po_data": {"po": {"expected_arrival_iso": "2026-07-10"}}},
            {"po": {"expected_arrival_iso": "2026-07-10"}},
            {
                "po_data": {"po": {"expected_arrival_iso": "2026-07-10"}},
                "po": {"expected_arrival_iso": "2026-08-01"},
            },
        ):
            with self.subTest(payload=payload):
                original = deepcopy(payload)
                self.po.source_data = payload
                with self.assertNumQueries(0):
                    self.assertEqual(
                        get_payable_expected_arrival(self.po), date(2026, 7, 10),
                    )
                self.assertEqual(self.po.source_data, original)

        self.po.source_data = {
            "po_data": {"po": {
                "shipping_date_iso": "2026-07-01",
                "order_date_iso": "2026-06-01",
            }},
        }
        self.assertIsNone(get_payable_expected_arrival(self.po))
        self.invoice.source_data = {"po": {"expected_arrival_iso": "2026-07-10"}}
        self.assertIsNone(get_payable_expected_arrival(self.invoice))

    def test_receivable_dates_due_filters_and_order_are_unchanged(self):
        today = timezone.localdate()
        added = []
        for number, due in enumerate((today - timedelta(days=1), None), 2):
            document = GeneratedDocument.objects.create(
                order=self.order,
                document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
                document_number=f"PORTAL-INVOICE-{number:03}",
                generated_by=self.user,
                source_data={"po": {"expected_arrival_iso": "2026-07-10"}},
            )
            added.append(SettlementAccount.objects.create(
                document=document,
                issue_date=today - timedelta(days=5),
                due_date=due,
                original_amount=Decimal("100.00"),
            ))
        overdue, no_due = added
        url = reverse("portal:settlement_receivables")
        response = self.client.get(url, {"sort": "arrival_asc"})

        self.assert_account_order(response, [no_due, overdue, self.receivable])
        self.assertContains(response, "<th>截止日期</th>", html=True)
        self.assertContains(response, 'name="due"')
        self.assertContains(response, self.receivable.due_date.isoformat())
        self.assertNotContains(response, "预计到货")
        self.assertNotContains(response, 'name="sort"')
        for account in response.context["page_obj"]:
            self.assertIsNone(account.portal_expected_arrival)
        for due, expected in (
            ("overdue", [overdue]),
            ("due_30", [self.receivable]),
            ("no_due", [no_due]),
        ):
            with self.subTest(due=due):
                self.assert_account_order(
                    self.client.get(url, {"due": due}), expected,
                )
