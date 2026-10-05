from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from documents.models import GeneratedDocument
from orders.models import Order, OrderItem
from portal.services.settlement_portal_service import (
    build_order_batch_amount_rows,
    build_settlement_comparison_context,
    build_settlement_home_context,
)
from shipments.models import ShipmentBatch


class SettlementComparisonPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="settlement-comparison-page",
            email="comparison-page@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)
        self.request_factory = RequestFactory()

    def create_order(self, bon, *, quantity=2, unit_price="100.25"):
        order = Order.objects.create(
            bon_de_commande=bon,
            hospital_name=f"Hospital {bon}",
            hospital_order_pdf=f"hospital_orders/{bon}.pdf",
            created_by=self.user,
        )
        OrderItem.objects.create(
            order=order,
            product_code=f"PRODUCT-{bon}",
            requested_quantity=quantity,
            hospital_unit_price=Decimal(unit_price),
        )
        return order

    def create_batch(self, order, batch_number):
        return ShipmentBatch.objects.create(
            order=order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=batch_number,
            month_key="2026-10",
        )

    def create_document(self, batch, document_type, amount):
        payload_key = (
            "invoice_data"
            if document_type
            == GeneratedDocument.DocumentType.HOSPITAL_INVOICE
            else "po_data"
        )
        return GeneratedDocument.objects.create(
            order=batch.order,
            shipment_batch=batch,
            document_type=document_type,
            document_number=(
                f"{document_type.upper()}-{batch.order_id}-B"
                f"{batch.batch_number}"
            ),
            source_data={
                payload_key: {
                    "totals": {
                        "total_raw": str(amount),
                    }
                }
            },
            generated_by=self.user,
        )

    def request(self, path, params=None):
        request = self.request_factory.get(path, params or {})
        request.user = self.user
        request.session = {}
        return request

    def test_home_links_to_full_comparison_and_still_limits_orders(self):
        for index in range(1, 17):
            self.create_batch(
                self.create_order(str(100000 + index)),
                1,
            )

        response = self.client.get(reverse("portal:settlement_home"))

        self.assertContains(response, "查看全部金额对照")
        self.assertContains(
            response,
            reverse("portal:settlement_comparison"),
        )
        rows = response.context["order_batch_amount_rows"]
        self.assertEqual(
            sum(row["is_order_start"] for row in rows),
            15,
        )
        self.assertNotIn(
            "100001",
            [row["order_number"] for row in rows],
        )

    def test_full_page_is_not_limited_to_fifteen_orders(self):
        for index in range(1, 17):
            self.create_batch(
                self.create_order(str(110000 + index)),
                1,
            )

        response = self.client.get(
            reverse("portal:settlement_comparison")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["order_count"], 16)
        self.assertEqual(
            sum(row["is_order_start"] for row in response.context["rows"]),
            16,
        )
        self.assertContains(response, "返回发票与结算")

    def test_pagination_is_by_order_and_keeps_all_eight_batches_together(self):
        orders = []

        for index in range(1, 22):
            order = self.create_order(str(120000 + index))
            self.create_batch(order, 1)
            orders.append(order)

        largest_order = orders[-1]
        for batch_number in range(2, 9):
            self.create_batch(largest_order, batch_number)

        first_page = self.client.get(
            reverse("portal:settlement_comparison")
        )
        second_page = self.client.get(
            reverse("portal:settlement_comparison"),
            {"page": 2},
        )

        first_rows = first_page.context["rows"]
        largest_rows = [
            row
            for row in first_rows
            if row["order_id"] == largest_order.id
        ]

        self.assertEqual(
            [row["batch_number"] for row in largest_rows],
            list(range(1, 9)),
        )
        self.assertEqual(
            len({row["order_id"] for row in first_rows}),
            20,
        )
        self.assertEqual(
            len({row["order_id"] for row in second_page.context["rows"]}),
            1,
        )
        self.assertNotIn(
            largest_order.id,
            {
                row["order_id"]
                for row in second_page.context["rows"]
            },
        )

    def test_exact_and_partial_bon_search_include_complete_order_groups(self):
        exact_order = self.create_order("150222")
        for batch_number in [3, 1, 2]:
            self.create_batch(exact_order, batch_number)

        partial_order = self.create_order("150333")
        self.create_batch(partial_order, 1)
        self.create_batch(self.create_order("999999"), 1)

        exact_response = self.client.get(
            reverse("portal:settlement_comparison"),
            {"q": "150222"},
        )
        partial_response = self.client.get(
            reverse("portal:settlement_comparison"),
            {"q": "150"},
        )

        self.assertEqual(
            {
                row["order_id"]
                for row in exact_response.context["rows"]
            },
            {exact_order.id},
        )
        self.assertEqual(
            [
                row["batch_number"]
                for row in exact_response.context["rows"]
            ],
            [1, 2, 3],
        )
        self.assertEqual(
            {
                row["order_id"]
                for row in partial_response.context["rows"]
            },
            {exact_order.id, partial_order.id},
        )

    def test_search_pagination_preserves_query(self):
        for index in range(1, 22):
            self.create_batch(
                self.create_order(str(150000 + index)),
                1,
            )

        response = self.client.get(
            reverse("portal:settlement_comparison"),
            {"q": "15"},
        )

        self.assertEqual(
            response.context["query_without_page"],
            "q=15",
        )
        self.assertContains(response, "q=15&page=2")

    def test_statistics_cover_entire_filtered_result(self):
        first_order = self.create_order("150222")
        first_batches = [
            self.create_batch(first_order, 1),
            self.create_batch(first_order, 2),
        ]
        second_order = self.create_order("150333")
        second_batch = self.create_batch(second_order, 1)
        self.create_batch(self.create_order("999999"), 1)

        self.create_document(
            first_batches[0],
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "300.00",
        )
        self.create_document(
            first_batches[0],
            GeneratedDocument.DocumentType.FACTORY_PO,
            "120.00",
        )
        self.create_document(
            first_batches[1],
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "200.00",
        )
        self.create_document(
            second_batch,
            GeneratedDocument.DocumentType.FACTORY_PO,
            "80.00",
        )

        response = self.client.get(
            reverse("portal:settlement_comparison"),
            {"q": "150"},
        )

        self.assertEqual(
            response.context["summary"],
            {
                "order_count": 2,
                "batch_count": 3,
                "invoice_count": 2,
                "po_count": 2,
            },
        )

    def test_home_and_full_page_share_identical_amount_rows(self):
        order = self.create_order(
            "155672",
            quantity=3,
            unit_price="100.10",
        )
        batch = self.create_batch(order, 1)
        self.create_document(
            batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "300.30",
        )
        self.create_document(
            batch,
            GeneratedDocument.DocumentType.FACTORY_PO,
            "144.00",
        )

        home_context = build_settlement_home_context(
            self.request("/portal/settlements/")
        )
        comparison_context = build_settlement_comparison_context(
            self.request("/portal/settlements/comparison/")
        )
        home_row = home_context["order_batch_amount_rows"][0]
        comparison_row = comparison_context["rows"][0]

        for key in [
            "order_total",
            "invoice_amount",
            "factory_po_amount",
            "status_text",
        ]:
            self.assertEqual(home_row[key], comparison_row[key])

        self.assertEqual(
            home_row["invoice"].document_number,
            comparison_row["invoice"].document_number,
        )
        self.assertEqual(
            home_row["factory_po"].document_number,
            comparison_row["factory_po"].document_number,
        )

    def test_full_page_uses_numeric_bon_descending_order(self):
        for bon in ["99999", "155141", "UPLOAD-ABC", "155672"]:
            self.create_batch(self.create_order(bon), 1)

        response = self.client.get(
            reverse("portal:settlement_comparison")
        )

        self.assertEqual(
            [
                row["order_number"]
                for row in response.context["rows"]
                if row["is_order_start"]
            ],
            ["155672", "155141", "99999", "UPLOAD-ABC"],
        )


    def test_missing_document_link_and_full_context_query_count(self):
        order = self.create_order("155672")
        batch = self.create_batch(order, 1)
        invoice = self.create_document(
            batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "300.00",
        )

        with self.assertNumQueries(7):
            context = build_settlement_comparison_context(
                self.request("/portal/settlements/comparison/")
            )

        row = context["rows"][0]
        self.assertEqual(
            row["invoice_url"],
            reverse("portal:document_detail", args=[invoice.id]),
        )
        self.assertIsNone(row["factory_po"])
        self.assertEqual(row["status_text"], "待生成")

        response = self.client.get(
            reverse("portal:settlement_comparison")
        )
        self.assertContains(response, invoice.document_number)
        self.assertContains(response, row["invoice_url"])
        self.assertContains(response, "未生成")
        self.assertContains(response, "待生成")

