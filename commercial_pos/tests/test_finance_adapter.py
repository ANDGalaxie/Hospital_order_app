"""Frozen batch finance: original formulas, correct cohorts and safe DTOs."""
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection, models
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.generation_service import build_commercial_snapshot
from commercial_pos.tests.fixtures import FixtureMixin, make_factory_po
from commercial_pos.tests import test_showcase
from documents.models import GeneratedDocument
from factory_confirmations.models import FactoryConfirmation, SerialItem
from finance.services.finance_analysis_service import calculate_hospital_revenue
from finance.services.settlement_finance_service import add_accrual_amount, build_accrual_chart, calculate_accrual_totals, format_percent
from portal.role_access import HOSPITAL_GROUP
from portal.services.commercial_showcase_service import build_commercial_finance_context
from shipments.models import ShipmentBatch, ShipmentBatchItem
from settlements.models import SettlementAccount


class CommercialFinanceTests(FixtureMixin, TestCase):
    make_document = test_showcase.ShowcaseSecurityTests.make_document

    def setUp(self):
        super().setUp()
        self.viewer = get_user_model().objects.create_user(username="Claire")
        self.viewer.groups.add(Group.objects.get_or_create(name=HOSPITAL_GROUP)[0])
        self.viewer.user_permissions.add(Permission.objects.get(content_type__app_label="commercial_pos", codename="view_commercial_showcase"))
        self.client.force_login(self.viewer)
        self.order_item.requested_quantity = self.order_item.confirmed_quantity = 20
        self.batch_item.shipped_quantity = 7
        self.serial.raw_data = {"delivered_quantity": 7}
        for item in (self.order_item, self.batch_item, self.serial):
            item.save()
        self.factory_po = make_factory_po(self.user, self.batch, number="DELAHK-COST-PRIVATE")
        self.po = self.make_document("commercial_po", "CPO-147891-B1", build_commercial_snapshot(self.batch))
        self.invoice = self.make_document("hospital_invoice", "Invoice B1", self.invoice_source(self.batch, 7, "1890.00"))
        self.url = reverse("portal:commercial_finance")

    def invoice_source(self, batch, quantity, amount, *, lines=True):
        payload = {"invoice": {"invoice_date": batch.batch_date.strftime("%d/%m/%Y")},
                   "totals": {"total_raw": amount, "total_units_raw": quantity}}
        if lines:
            payload["items"] = [{"product_code": self.order_item.product_code, "quantity_raw": quantity, "unit_price_raw": "270.00", "amount_raw": amount}]
        return {"order_id": self.order.pk, "shipment_batch_id": batch.pk, "invoice_data": payload}

    def page(self, **filters):
        response = self.client.get(self.url, filters)
        self.assertEqual(response.status_code, 200)
        return response

    def second_batch(self):
        confirmation = FactoryConfirmation.objects.create(order=self.order, factory=self.order.factory, shipping_date=date(2026, 5, 2), created_by=self.user)
        batch = ShipmentBatch.objects.create(order=self.order, factory_confirmation=confirmation, batch_number=2, batch_date=confirmation.shipping_date, month_key="2026-05")
        ShipmentBatchItem.objects.create(batch=batch, product=self.order_item.product, product_code=self.order_item.product_code, shipped_quantity=3)
        SerialItem.objects.create(order=self.order, factory_confirmation=confirmation, product=self.order_item.product, product_code=self.order_item.product_code,
                                  serial_number="SECOND-BATCH", expiration_date=date(2028, 1, 1), raw_data={"delivered_quantity": 3})
        make_factory_po(self.user, batch)
        self.make_document("commercial_po", "CPO-147891-B2", build_commercial_snapshot(batch), batch=batch)
        self.make_document("hospital_invoice", "Invoice B2", self.invoice_source(batch, 3, "810.00"), batch=batch)
        return batch

    def test_reuses_original_formulas_and_keeps_order_amount_separate_from_invoice(self):
        with (patch("portal.services.commercial_finance_service.calculate_hospital_revenue", wraps=calculate_hospital_revenue) as revenue,
             patch("portal.services.commercial_finance_service.calculate_accrual_totals", wraps=calculate_accrual_totals) as accrual):
            response = self.page()
        self.assertTrue(revenue.called and accrual.called)
        summary = response.context["summary"]
        self.assertEqual((summary["sales_total"], summary["purchase_total"], summary["gross_profit"]), ("1890.00", "1120.00", "770.00"))
        self.assertEqual(response.context["original_order_total"], "5 400.00 €")
        self.assertEqual(response.context["order_rows"][0]["sales"], "1 890.00 €")
        self.assertEqual(response.context["kpi_cards"][3]["value"], "40.74%")
        self.assertEqual(response.context["monthly_rows"][0]["gross_profit"], "770.00 €")
        self.assertEqual(response.context["hospital_rows"][0]["sales"], "1 890.00 €")
        self.assertEqual(response.context["product_rows"][0]["sales"], "1 890.00 €")
        self.assertEqual(response.context["chart_data"]["accrual"], {"labels": ["2026-04"], "sales": [1890.0], "purchases": [1120.0], "gross_profit": [770.0]})

    def test_multiple_batches_reconcile_months_hospital_and_deduplicate_original_order(self):
        self.second_batch()
        response = self.page()
        self.assertEqual({key: response.context["summary"][key] for key in ("sales_total", "purchase_total", "gross_profit")},
                         {"sales_total": "2700.00", "purchase_total": "1600.00", "gross_profit": "1100.00"})
        self.assertEqual([r["batch_number"] for r in response.context["order_rows"]], [1, 2])
        self.assertEqual([r["order_amount"] for r in response.context["order_rows"]], ["5 400.00 €", "—"])
        self.assertEqual(response.context["original_order_total"], "5 400.00 €")
        self.assertEqual(response.context["chart_data"]["accrual"]["sales"], [1890.0, 810.0])
        self.assertEqual(response.context["hospital_rows"][0]["count"], 2)
        self.assertEqual(response.context["hospital_rows"][0]["gross_profit"], "1 100.00 €")

    def test_newest_invoice_per_batch_is_counted_once_without_recomputing_its_price(self):
        revised = self.make_document("hospital_invoice", "Invoice B1 revision", self.invoice_source(self.batch, 7, "1750.00"))
        GeneratedDocument.objects.filter(pk__in=[self.invoice.pk, revised.pk]).update(generated_at=revised.generated_at)
        response = self.page()
        self.assertEqual(response.context["summary"]["sales_total"], "1750.00")
        self.assertEqual(response.context["summary"]["gross_profit"], "630.00")
        self.assertEqual(response.context["summary"]["matched_batch_count"], 1)
        self.assertEqual(len(response.context["order_rows"]), 1)
        self.assertEqual(response.context["order_rows"][0]["invoice_url"], reverse("portal:commercial_document_file", args=[revised.pk, "pdf"]))

    def test_invoice_missing_never_uses_order_or_shipment_revenue_as_invoiced_sales(self):
        self.invoice.delete()
        response = self.page()
        self.assertIsNone(response.context["summary"]["sales_total"])
        self.assertEqual(response.context["order_rows"][0]["purchases"], "1 120.00 €")
        self.assertEqual(response.context["order_rows"][0]["sales"], "—")
        self.assertEqual(response.context["order_rows"][0]["gross_profit"], "—")
        self.assertEqual(response.context["summary"]["unmatched_batch_count"], 1)
        self.assertFalse(response.context["chart_data"]["accrual"]["labels"])

    def test_purchase_missing_never_falls_back_to_factory_po_or_live_prices(self):
        self.po.delete()
        response = self.page()
        self.assertEqual(response.context["order_rows"][0]["sales"], "1 890.00 €")
        self.assertEqual(response.context["order_rows"][0]["purchases"], "—")
        self.assertEqual(response.context["order_rows"][0]["gross_profit"], "—")
        self.assertIsNone(response.context["summary"]["purchase_total"])
        self.assertEqual(response.context["original_order_total"], "5 400.00 €")

    def test_unmatched_revenue_and_purchase_from_different_batches_are_not_combined(self):
        batch = self.second_batch()
        self.invoice.delete()
        GeneratedDocument.objects.filter(shipment_batch=batch, document_type="commercial_po").delete()
        response = self.page()
        self.assertEqual(response.context["summary"]["matched_batch_count"], 0)
        self.assertIsNone(response.context["summary"]["gross_profit"])
        self.assertEqual(len(response.context["order_rows"]), 2)
        self.assertEqual([r["gross_profit"] for r in response.context["order_rows"]], ["—", "—"])

    def test_wrong_binding_or_damaged_latest_invoice_cannot_revive_older_invoice(self):
        for amount, currency, wrong_batch in (("NaN", "EUR", False), ("Infinity", "EUR", False), ("-1", "EUR", False), ("1890.00", "USD", False), ("1890.00", "EUR", True)):
            with self.subTest(amount=amount, currency=currency, wrong_batch=wrong_batch):
                source = self.invoice_source(self.batch, 7, amount)
                source["currency"] = currency
                if wrong_batch:
                    source["shipment_batch_id"] += 999
                latest = self.make_document("hospital_invoice", f"Invalid {amount} {currency} {wrong_batch}", source)
                response = self.page()
                self.assertEqual(response.context["summary"]["matched_batch_count"], 0)
                self.assertGreater(response.context["ignored_count"], 0)
                self.assertEqual(response.context["order_rows"][0]["sales"], "—")
                latest.delete()

    def test_frozen_values_survive_current_price_policy_catalog_factory_and_settlement_changes(self):
        SettlementAccount.objects.create(document=self.factory_po, issue_date=self.batch.batch_date, original_amount=Decimal("100.00"), counterparty_name="INTERNAL-PAYABLE-PRIVATE")
        original = self.page().context["summary"]
        self.order_item.product.hospital_unit_price = 999
        self.order_item.product.factory_unit_price = 987654
        self.order_item.product.save()
        CommercialPOPricePolicy.objects.update(unit_price=999)
        self.assertEqual(SettlementAccount.objects.update(original_amount=987654), 1)
        self.factory_po.source_data["po_data"]["totals"]["total_raw"] = "987654.32"
        self.factory_po.save(update_fields=["source_data"])
        with (patch("finance.services.finance_analysis_service.get_item_prices", side_effect=AssertionError("No factory snapshot")),
             patch("finance.services.settlement_finance_service.build_filtered_account_queryset", side_effect=AssertionError("No settlement queries")),
             patch("commercial_pos.services.generation_service.ensure_commercial_po_for_batch", side_effect=AssertionError("No generation"))):
            response = self.page()
        self.assertEqual(response.context["summary"], original)

    def test_invoice_total_not_product_line_sum_drives_accrual_and_unknown_lines_are_not_allocated(self):
        self.invoice.source_data["invoice_data"]["totals"]["total_raw"] = "2000.00"
        self.invoice.save(update_fields=["source_data"])
        response = self.page()
        self.assertEqual(response.context["summary"]["sales_total"], "2000.00")
        self.assertEqual(response.context["summary"]["gross_profit"], "880.00")
        self.assertEqual(response.context["product_rows"][0]["sales"], "1 890.00 €")
        self.invoice.source_data["invoice_data"].pop("items")
        self.invoice.save(update_fields=["source_data"])
        response = self.page()
        self.assertEqual(response.context["summary"]["sales_total"], "2000.00")
        self.assertEqual(response.context["product_rows"][0]["sales"], "—")
        self.assertEqual(response.context["product_rows"][0]["gross_profit"], "—")

    def test_zero_revenue_negative_profit_and_zero_margin_reuse_internal_formula(self):
        self.invoice.source_data["invoice_data"]["totals"]["total_raw"] = "0.00"
        self.invoice.source_data["invoice_data"]["items"][0].update(unit_price_raw="0.00", amount_raw="0.00")
        self.invoice.save(update_fields=["source_data"])
        response = self.page()
        self.assertEqual(response.context["summary"]["gross_profit"], "-1120.00")
        self.assertEqual(response.context["kpi_cards"][3]["value"], "0.00%")

    def test_frozen_shipping_filters_and_order_hospital_filters_have_same_scope(self):
        self.second_batch()
        self.batch.batch_date = date(2099, 1, 1)
        self.batch.save(update_fields=["batch_date"])
        response = self.page(date_from="2026-04-22", date_to="2026-04-22", hospital="Synthetic", order="147891")
        self.assertEqual(response.context["summary"]["sales_total"], "1890.00")
        self.assertEqual(len(response.context["order_rows"]), 1)
        response = self.page(date_from="2026-05-01")
        self.assertEqual(response.context["summary"]["sales_total"], "810.00")
        self.assertTrue(self.page(date_from="2026-13-01").context["filter_errors"])

    def test_context_html_json_and_links_expose_no_real_factory_cost_or_settlement_objects(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.page()
        sql = "\n".join(q["sql"] for q in queries)
        for table in ("settlements_settlementaccount", "settlements_paymenttransaction", "pricing_pricepolicy", "factory_confirmations_serialitem"):
            self.assertNotIn(table, sql)
        self.assertNotIn("'factory_po'", sql)
        self.assertFalse([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        data = {key: response.context[key] for key in ("summary", "cards", "kpi_cards", "monthly_rows", "hospital_rows", "product_rows", "order_rows", "groups", "chart_data")}
        serialized = json.dumps(data, default=str)
        for forbidden in ("DELAHK-COST-PRIVATE", "factory_po", "factory_unit_price", "discount_rate", "expiration_threshold", "price_policy", "source_data", "987654", "/portal/settlements/", "/portal/factory/", "/portal/finance/", "/admin/"):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, response.content.decode())
        def check(value):
            self.assertNotIsInstance(value, models.Model)
            if isinstance(value, dict):
                for item in value.values(): check(item)
            elif isinstance(value, (list, tuple)):
                for item in value: check(item)
        check(data)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertEqual(len(soup.select(".kpi-card")), 10)
        self.assertEqual([card["value"] for card in response.context["kpi_cards"]][4:], ["—"] * 6)
        chart = json.loads(soup.select_one("#finance-chart-data").string)
        self.assertEqual(chart["cash"]["labels"], [])
        self.assertEqual(chart["accrual"]["purchases"], [1120.0])
        self.assertFalse(soup.select('a[href*="export"]'))
        for key in ("invoice_url", "purchase_url"):
            document_response = self.client.get(response.context["order_rows"][0][key])
            self.assertEqual(document_response.status_code, 200)
            self.assertTrue(b"".join(document_response.streaming_content))
            document_response.close()
        self.assertEqual(self.client.get("/portal/finance/export.xlsx").status_code, 403)
        self.assertEqual(self.client.get(reverse("portal:commercial_document_file", args=[self.factory_po.pk, "pdf"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal:commercial_document_file", args=[self.po.pk, "json"])).status_code, 404)

    def test_unbatched_invoice_is_not_copied_into_each_shipment_batch(self):
        self.second_batch()
        unbound = self.make_document("hospital_invoice", "Order-wide invoice", {"invoice_data": {"totals": {"total_raw": "999999.00"}}})
        unbound.shipment_batch = None
        unbound.save(update_fields=["shipment_batch"])
        response = self.page()
        self.assertEqual(response.context["summary"]["sales_total"], "2700.00")
        self.assertEqual(response.context["summary"]["matched_batch_count"], 2)

    def test_newest_invoice_with_wrong_order_never_changes_batch_identity(self):
        other = type(self.order).objects.create(bon_de_commande="OTHER-PRIVATE", hospital_name="OTHER-PRIVATE", factory=self.order.factory, created_by=self.user)
        bad = self.make_document("hospital_invoice", "Wrong order", self.invoice_source(self.batch, 7, "1890.00"))
        bad.order = other
        bad.save(update_fields=["order"])
        response = self.page()
        self.assertEqual(response.context["summary"]["matched_batch_count"], 0)
        self.assertEqual(response.context["order_rows"][0]["label"], "147891")
        self.assertNotContains(response, "OTHER-PRIVATE")

    def test_three_languages_translate_the_financial_basis_and_keep_ten_cards(self):
        labels = {
            "zh-hans": ["医院已开票金额", "采购订单金额", "预计毛利", "预计毛利率"],
            "en": ["Hospital invoiced amount", "Purchase order amount", "Estimated gross profit", "Estimated gross margin"],
            "fr": ["Montant facturé aux hôpitaux", "Montant des bons de commande", "Marge brute estimée", "Taux de marge brute estimé"],
        }
        statistics = {
            "zh-hans": ["已生成采购订单", "医院订单数", "产品总件数", "采购订单总金额"],
            "en": ["Generated purchase orders", "Hospital order count", "Total product units", "Total purchase order amount"],
            "fr": ["Bons de commande générés", "Nombre de commandes hospitalières", "Nombre total d’unités", "Montant total des bons de commande"],
        }
        for language in ("zh-hans", "en", "fr"):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
            response = self.page()
            soup = BeautifulSoup(response.content, "html.parser")
            self.assertEqual(len(soup.select(".kpi-card")), 10)
            self.assertEqual([card["label"] for card in response.context["kpi_cards"][:4]], labels[language])
            self.assertEqual([card["label"] for card in response.context["cards"]], statistics[language])
            help_text = [card["help"] for card in response.context["kpi_cards"]]
            self.assertEqual(len(set(help_text)), 10)
            self.assertTrue(all(help_text))
            self.assertEqual([node.get_text(strip=True) for node in soup.select(".kpi-help")], help_text)
            self.assertEqual(response.context["chart_data"]["ui"]["purchases"], {"zh-hans": "采购额", "en": "Purchases", "fr": "Achats"}[language])
            self.assertEqual(response.context["chart_data"]["ui"]["gross_profit"], labels[language][2])
            for term in ("Commercial", "Demo", "商业展示", "只读查看", "已生成新版 PO", "对应 BON", "新版"):
                self.assertNotIn(term, response.content.decode())
                self.assertNotIn(term, json.dumps(response.context["cards"], ensure_ascii=False))
            self.assertNotIn("commercial po", soup.get_text().casefold())
            if language != "zh-hans":
                self.assertNotRegex(soup.get_text().replace("中文", ""), r"[\u4e00-\u9fff]")


class SharedFinanceFormulaTests(TestCase):
    def test_rounding_grouping_negative_profit_and_margin_match_original_contract(self):
        self.assertEqual(calculate_hospital_revenue(1, "1.005"), Decimal("1.01"))
        self.assertEqual(calculate_accrual_totals("0", "12.34")["gross_profit"], Decimal("-12.34"))
        self.assertEqual(calculate_accrual_totals("0", "12.34")["gross_margin"], Decimal("0.00"))
        self.assertEqual(format_percent(calculate_accrual_totals("100", "65")["gross_margin"]), Decimal("35.00"))
        group = {}
        add_accrual_amount(group, "2026-05", sales="100", purchases="65")
        add_accrual_amount(group, "2026-05", sales="50", purchases="20")
        add_accrual_amount(group, "2026-04", sales="1.005", purchases="0")
        self.assertEqual(build_accrual_chart(group), {"labels": ["2026-04", "2026-05"], "sales": [1.01, 150.0], "purchases": [0.0, 85.0], "gross_profit": [1.01, 65.0]})
