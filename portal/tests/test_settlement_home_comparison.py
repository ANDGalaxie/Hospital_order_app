from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from documents.models import GeneratedDocument
from orders.models import Order, OrderItem
from portal.services.settlement_portal_service import (
    build_order_batch_amount_rows,
)
from shipments.models import ShipmentBatch


class SettlementHomeAmountComparisonTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="settlement-comparison",
            email="comparison@example.com",
            password="test-password",
        )
        self.client.force_login(self.user)

    def create_order(
        self,
        bon,
        *,
        quantity=2,
        hospital_unit_price=Decimal("100.25"),
    ):
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
            hospital_unit_price=hospital_unit_price,
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
        source_data = {
            payload_key: {
                "totals": {
                    "total_raw": str(amount),
                }
            }
        }
        return GeneratedDocument.objects.create(
            order=batch.order,
            shipment_batch=batch,
            document_type=document_type,
            document_number=(
                f"{document_type.upper()}-{batch.order_id}-B"
                f"{batch.batch_number}"
            ),
            source_data=source_data,
            generated_by=self.user,
        )

    def test_home_replaces_recent_accounts_with_comparison_columns(self):
        order = self.create_order("155141")
        batch = self.create_batch(order, 1)
        self.create_document(
            batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "3000.00",
        )
        self.create_document(
            batch,
            GeneratedDocument.DocumentType.FACTORY_PO,
            "1440.00",
        )

        response = self.client.get(reverse("portal:settlement_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "订单 / 批次金额对照")
        self.assertNotContains(response, "最近结算账户")
        self.assertContains(response, "医院订单总额")
        self.assertContains(response, "Hospital Invoice")
        self.assertContains(response, "Factory PO")
        self.assertNotContains(response, "差价")
        self.assertNotContains(response, "利润")

    def test_order_group_and_batches_are_complete_and_ascending(self):
        order = self.create_order("155141")
        for batch_number in [3, 1, 2]:
            self.create_batch(order, batch_number)

        rows = build_order_batch_amount_rows()

        self.assertEqual(
            [row["batch_number"] for row in rows],
            [1, 2, 3],
        )
        self.assertEqual(rows[0]["order_number"], "155141")
        self.assertEqual(rows[0]["order_total"], Decimal("200.50"))
        self.assertTrue(rows[0]["is_order_start"])
        self.assertEqual(rows[1]["order_number"], "")
        self.assertIsNone(rows[1]["order_total"])
        self.assertEqual(rows[2]["order_number"], "")
        self.assertIsNone(rows[2]["order_total"])

    def test_frozen_document_amounts_links_and_missing_status(self):
        order = self.create_order("155672")
        complete_batch = self.create_batch(order, 1)
        pending_batch = self.create_batch(order, 2)
        invoice = self.create_document(
            complete_batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "3000.00",
        )
        factory_po = self.create_document(
            complete_batch,
            GeneratedDocument.DocumentType.FACTORY_PO,
            "1440.00",
        )
        self.create_document(
            pending_batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "500.00",
        )

        rows = build_order_batch_amount_rows()

        self.assertEqual(rows[0]["invoice_amount"], Decimal("3000.00"))
        self.assertEqual(rows[0]["factory_po_amount"], Decimal("1440.00"))
        self.assertEqual(
            rows[0]["invoice_url"],
            reverse("portal:document_detail", args=[invoice.id]),
        )
        self.assertEqual(
            rows[0]["factory_po_url"],
            reverse("portal:document_detail", args=[factory_po.id]),
        )
        self.assertEqual(rows[0]["status_text"], "已生成")
        self.assertIsNone(rows[1]["factory_po"])
        self.assertEqual(rows[1]["status_text"], "待生成")

        response = self.client.get(reverse("portal:settlement_home"))
        self.assertContains(response, "未生成")
        self.assertContains(response, "待生成")

    def test_numeric_bon_descending_and_non_numeric_last(self):
        for bon in ["99999", "155141", "UPLOAD-ABC", "155672"]:
            self.create_batch(self.create_order(bon), 1)

        rows = build_order_batch_amount_rows()

        self.assertEqual(
            [row["order_number"] for row in rows],
            ["155672", "155141", "99999", "UPLOAD-ABC"],
        )

    def test_only_latest_fifteen_orders_but_keeps_all_selected_batches(self):
        orders = []

        for index in range(1, 17):
            order = self.create_order(str(100000 + index))
            self.create_batch(order, 1)
            orders.append(order)

        selected_order = orders[-1]
        self.create_batch(selected_order, 2)
        self.create_batch(selected_order, 3)

        rows = build_order_batch_amount_rows()
        shown_orders = [row["order_number"] for row in rows if row["order_number"]]

        self.assertEqual(len(shown_orders), 15)
        self.assertNotIn("100001", shown_orders)
        selected_rows = [
            row
            for row in rows
            if row["order_number"] == selected_order.bon_de_commande
            or (
                not row["order_number"]
                and row in rows[:3]
            )
        ]
        self.assertEqual(
            [row["batch_number"] for row in selected_rows],
            [1, 2, 3],
        )

    def test_invalid_order_price_or_broken_frozen_amount_is_data_error(self):
        invalid_order = self.create_order(
            "155673",
            hospital_unit_price=Decimal("0.00"),
        )
        self.create_batch(invalid_order, 1)

        broken_order = self.create_order("155672")
        broken_batch = self.create_batch(broken_order, 1)
        self.create_document(
            broken_batch,
            GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            "300.00",
        )
        broken_po = self.create_document(
            broken_batch,
            GeneratedDocument.DocumentType.FACTORY_PO,
            "120.00",
        )
        broken_po.source_data = {"po_data": {"totals": {}}}
        broken_po.save(update_fields=["source_data"])

        rows = build_order_batch_amount_rows()

        self.assertEqual(rows[0]["status_text"], "数据异常")
        self.assertEqual(rows[1]["status_text"], "数据异常")
        self.assertIsNone(rows[1]["factory_po_amount"])

    def test_comparison_builder_uses_fixed_number_of_queries(self):
        order = self.create_order("155672")

        for batch_number in [1, 2, 3]:
            batch = self.create_batch(order, batch_number)
            self.create_document(
                batch,
                GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
                Decimal("300.00") * batch_number,
            )
            self.create_document(
                batch,
                GeneratedDocument.DocumentType.FACTORY_PO,
                Decimal("120.00") * batch_number,
            )

        with self.assertNumQueries(5):
            rows = build_order_batch_amount_rows()

        self.assertEqual(len(rows), 3)
