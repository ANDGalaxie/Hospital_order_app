"""Operations detail: frozen purchase values, business wording and read isolation."""
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.generation_service import build_commercial_snapshot
from documents.models import GeneratedDocument
from documents.services.document_generation_service import render_po_html
from factory_confirmations.models import SerialItem
from orders.models import OrderItem
from portal.services.commercial_showcase_service import build_showcase_batch_context
from products.models import Product
from shipments.models import ShipmentBatchItem

from .fixtures import FixtureMixin, make_factory_po
from . import test_showcase


class OperationsDetailTests(FixtureMixin, TestCase):
    make_document = test_showcase.ShowcaseSecurityTests.make_document

    def setUp(self):
        super().setUp()
        self.viewer = get_user_model().objects.create_user(username="hospital-viewer")
        self.viewer.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(self.viewer)
        product = self.order_item.product
        product.code = self.order_item.product_code = self.batch_item.product_code = self.serial.product_code = "BMA-2.5015"
        self.order_item.requested_quantity = self.order_item.confirmed_quantity = self.batch_item.shipped_quantity = 7
        self.serial.raw_data = {"delivered_quantity": 7}
        for obj in (product, self.order_item, self.batch_item, self.serial):
            obj.save()
        for code, quantity in (("BMA-2.5020", 4), ("BMA-3.0065", 65)):
            other = Product.objects.create(code=code, description="Hospital product", factory=product.factory, category=product.category)
            OrderItem.objects.create(order=self.order, product=other, product_code=code, requested_quantity=quantity, confirmed_quantity=quantity,
                                     price_policy=self.order_item.price_policy, hospital_unit_price=Decimal("270.00"))
            ShipmentBatchItem.objects.create(batch=self.batch, product=other, product_code=code, shipped_quantity=quantity)
            SerialItem.objects.create(order=self.order, factory_confirmation=self.batch.factory_confirmation, product=other,
                                      product_code=code, serial_number=f'SERIAL-{code}', expiration_date=date(2026, 6, 1),
                                      raw_data={'delivered_quantity': quantity})
        self.factory_po = make_factory_po(self.user, self.batch)
        # Use an intentionally different frozen row order to catch reconstruction
        # from canonical/live items. This is test setup, never a production write.
        payload = self.factory_po.source_data["po_data"]
        payload["items"] = list(reversed(payload["items"]))
        # Match the real 147891 historical schema: canonical total_units
        # is frozen at source_data level; render totals have amount only.
        payload["totals"].pop("total_units_raw")
        payload["totals"].pop("total_units")
        self.factory_po.save(update_fields=["source_data"])
        Path(self.factory_po.html_file.path).write_text(render_po_html(
            po_data=payload, template_path=Path(settings.BASE_DIR) / "templates/factory_purchase_order.html"), encoding="utf-8")
        self.snapshot = build_commercial_snapshot(self.batch)
        self.po = self.make_document("commercial_po", self.snapshot["document_number"], self.snapshot)
        self.url = reverse("portal:commercial_operations_detail", args=[self.batch.pk])

    def page(self, language="zh-hans"):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        return response, soup

    def price_table(self, soup):
        return soup.select_one(".workflow-price-heading").find_parent("section").select_one("table")

    def test_exact_four_columns_and_frozen_147891_values_and_totals(self):
        response, soup = self.page()
        table = self.price_table(soup)
        self.assertEqual([n.get_text(strip=True) for n in table.select("thead th")], ["产品号", "本批数量", "采购单价", "采购金额"])
        actual = [[n.get_text(strip=True) for n in row.select("td")] for row in table.select("tbody tr")]
        self.assertEqual(actual, [[item["product_code"], str(int(Decimal(str(item["quantity_raw"])))),
                                  f"€{Decimal(str(item['unit_price_raw'])):,.2f}", f"€{Decimal(str(item['amount_raw'])):,.2f}"]
                                 for item in self.snapshot["po_data"]["items"]])
        self.assertIn(["BMA-2.5015", "7", "€160.00", "€1,120.00"], actual)
        self.assertIn(["BMA-2.5020", "4", "€160.00", "€640.00"], actual)
        self.assertEqual([n.get_text(strip=True) for n in soup.select(".workflow-price-summary > span")], ["本批数量:76", "采购总额:€12,160.00"])
        self.assertEqual(response.context["row"]["shipping_date"], date(2026, 4, 22))
        self.assertEqual(response.context["price_summary"]["quantity"], Decimal("76"))
        self.assertEqual(response.context["price_summary"]["amount"], "€12,160.00")
        self.assertEqual(len(soup.select(".serial-table-wrap")), 0)
        serial_table = soup.find(string="Serial Number").find_parent("table")
        self.assertEqual(len(serial_table.select("thead th")), 3)
        self.assertEqual(len(serial_table.select_one("tbody tr").select("td")), 3)

    def test_all_languages_have_business_wording_and_no_internal_cost_terms(self):
        languages = {
            "zh-hans": (["产品号", "本批数量", "采购单价", "采购金额"], ["本批数量", "采购总额"], "采购订单"),
            "en": (["Product code", "Batch quantity", "Purchase Unit Price", "Purchase Amount"], ["Batch quantity", "Purchase Total"], "Purchase Orders"),
            "fr": (["Référence produit", "Quantité du lot", "Prix unitaire d’achat", "Montant d’achat"], ["Quantité du lot", "Total des achats"], "Commandes d’achat"),
        }
        for language, (headers, summary, document_label) in languages.items():
            with self.subTest(language=language):
                response, soup = self.page(language)
                self.assertEqual([n.get_text(strip=True) for n in self.price_table(soup).select("thead th")], headers)
                self.assertEqual([n.contents[0].strip().removesuffix(":") for n in soup.select(".workflow-price-summary > span")], summary)
                self.assertIn(document_label, soup.get_text())
                for forbidden in ("只读查看", "商业展示", "Commercial", "Demo", "基础工厂价", "工厂实际金额", "临期优惠", "Factory PricePolicy"):
                    self.assertNotIn(forbidden, response.content.decode())
                for forbidden in ("commercial", "demo", "展示", "presentation", "preview", "只读", "hospital-facing"):
                    self.assertNotIn(forbidden, soup.get_text(" ", strip=True).lower())

    def test_missing_purchase_order_is_empty_in_all_languages_and_get_never_writes(self):
        self.po.delete()
        before = GeneratedDocument.objects.count()
        empty = {
            "zh-hans": "当前批次尚未生成采购订单。",
            "en": "No purchase order has been generated for this batch yet.",
            "fr": "Aucun bon de commande n’a encore été généré pour ce lot.",
        }
        for language, message in empty.items():
            with self.subTest(language=language), CaptureQueriesContext(connection) as queries:
                response, soup = self.page(language)
                self.assertContains(response, message)
                self.assertIsNone(self.price_table(soup))
                self.assertFalse(soup.select(".workflow-price-summary"))
                self.assertEqual(response.context["price_rows"], [])
                self.assertIsNone(response.context["price_summary"])
            self.assertFalse([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(GeneratedDocument.objects.count(), before)

    def test_frozen_purchase_payload_survives_live_prices_quantities_dates_and_factory_amount_changes(self):
        original, _ = self.page()
        ShipmentBatchItem.objects.filter(batch=self.batch).update(shipped_quantity=999)
        OrderItem.objects.filter(order=self.order).update(requested_quantity=999, hospital_unit_price=999)
        CommercialPOPricePolicy.objects.all().update(unit_price=999)
        self.batch.batch_date = date(2027, 1, 1)
        self.batch.save(update_fields=["batch_date"])
        self.factory_po.source_data["po_data"]["totals"]["total_raw"] = "987654.32"
        self.factory_po.save(update_fields=["source_data"])
        with patch("commercial_pos.services.price_service.resolve_commercial_price", side_effect=AssertionError("No live pricing")), CaptureQueriesContext(connection) as queries:
            response, _ = self.page()
        for key in ("price_rows", "price_summary"):
            self.assertEqual(response.context[key], original.context[key])
        self.assertEqual(response.context["row"]["shipping_date"], date(2026, 4, 22))
        self.assertFalse([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])

    def test_context_is_an_allowlisted_projection_with_no_factory_costs_models_or_payload(self):
        request = RequestFactory().get(self.url)
        request.user = self.viewer
        context = build_showcase_batch_context(request, self.batch.pk, operations=True)
        serialized = json.dumps(context, default=str)
        for forbidden in ("base_factory_total", "estimated_factory_total", "factory_discount_savings", "base_factory_unit_price",
                          "discounted_factory_unit_price", "expiration_discount_rate", "expiration_threshold_days", "price_policy",
                          "factory_po", "source_data", "po_data", "DELAHK", "Internal synthetic policy", "987654.32"):
            self.assertNotIn(forbidden, serialized)
        for row in context["price_rows"]:
            self.assertEqual(set(row), {"product_code", "quantity", "unit_price", "amount"})
        self.assertEqual(set(context["price_summary"]), {"quantity", "amount"})

    def test_wrong_batch_document_and_legacy_or_damaged_payload_do_not_fall_back_to_live_pricing(self):
        for source in ({}, {k: v for k, v in self.snapshot.items() if k != "po_data"}):
            with self.subTest(source_keys=list(source)):
                self.po.source_data = source
                self.po.save(update_fields=["source_data"])
                response, soup = self.page()
                self.assertContains(response, "当前批次尚未生成采购订单。")
                self.assertIsNone(self.price_table(soup))
        self.po.shipment_batch = None
        self.po.save(update_fields=["shipment_batch"])
        response, _ = self.page()
        self.assertContains(response, "当前批次尚未生成采购订单。")

    def test_visual_evidence_uses_unmodified_workflow_and_same_css(self):
        response, soup = self.page()
        for css_class in ("workflow-detail-grid", "portal-panel", "detail-info-grid", "workflow-doc-grid", "workflow-price-heading", "portal-table"):
            self.assertTrue(soup.select('.' + css_class))
        self.assertContains(response, 'portal/css/workflow.css')
        evidence = os.environ.get("OPERATIONS_DETAIL_EVIDENCE_DIR")
        if evidence:
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            for language in ("zh-hans", "en", "fr"):
                response, _ = self.page(language)
                (directory / f"operations_{language}.html").write_bytes(response.content)
            self.client.force_login(self.user)
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
            reference = self.client.get(reverse("portal:workflow_detail", args=[self.workflow.pk]))
            self.assertEqual(reference.status_code, 200)
            (directory / "workflow_reference.html").write_bytes(reference.content)
