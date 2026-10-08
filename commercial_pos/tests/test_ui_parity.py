"""Read-only UI/template regression tests, with synthetic batch-bound files."""
import hashlib
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from commercial_pos.services.generation_service import build_commercial_snapshot, render_commercial_files
from documents.models import GeneratedDocument
from documents.services.document_generation_service import render_po_html
from factory_confirmations.models import FactoryConfirmation, SerialItem
from shipments.models import ShipmentBatch, ShipmentBatchItem

from .fixtures import FixtureMixin, make_factory_po
from . import test_showcase


class CommercialUIParityTests(FixtureMixin, TestCase):
    make_document = test_showcase.ShowcaseSecurityTests.make_document

    def setUp(self):
        super().setUp()
        self.demo = get_user_model().objects.create_user(username="parity-demo")
        self.demo.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(self.demo)
        self.factory_po = make_factory_po(self.user, self.batch, number="DELAHK-PRIVATE")
        self.cpo = self.make_document("commercial_po", "CPO-147891-B1", build_commercial_snapshot(self.batch))
        self.invoice = self.make_document("hospital_invoice", "INVOICE-B1", {"invoice_data": {"totals": {"total_raw": "270.00"}}})


    def detail(self, batch, operations=False):
        return reverse("portal:commercial_operations_detail" if operations else "portal:commercial_purchase_detail", args=[batch.pk])

    def test_factory_list_structure_css_and_clickable_batch_bon(self):
        response = self.client.get(reverse("portal:commercial_purchase_orders"))
        for value in ("factory.css", "portal-page-header", "portal-header-actions", "portal-list-stats factory-stats",
                      "portal-list-filters portal-panel factory-filters", "portal-list-filter-form", "factory-table-wrap",
                      "portal-table factory-table clean-factory-table", "colgroup", "col-bon-ordinal", "status-pill", "mini-button"):
            self.assertContains(response, value)
        self.assertContains(response, f'href="{self.detail(self.batch)}"', count=3)
        self.assertNotContains(response, "commercial.css")
        self.assertNotIn('method="post"', response.content.decode().split('<main', 1)[1].split('</main>', 1)[0])

    def test_factory_detail_structure_and_readonly_serial_facts(self):
        response = self.client.get(self.detail(self.batch))
        for value in ("factory.css", "factory-detail-grid", "info-grid", "info-item", "panel-header", "debug-box",
                      "summary-table", "serial-table", self.serial.serial_number, "CPO-147891-B1"):
            self.assertContains(response, value)
        content = response.content.decode().split('<main', 1)[1].split('</main>', 1)[0]
        self.assertNotIn('method="post"', content)
        self.assertNotIn('<input', content)
        for value in ('/admin/', 're_extract', 'save_serials', '84.00', '120.00', 'DELAHK'):
            self.assertNotContains(response, value)

    def test_workflow_list_and_detail_structure_and_css(self):
        response = self.client.get(reverse("portal:commercial_operations"))
        for value in ("workflow.css", "workflow-stats", "workflow-filters portal-panel", "workflow-filter-form",
                      "workflow-table-wrap", "portal-table workflow-table", "doc-status", "small-link", "col-po"):
            self.assertContains(response, value)
        self.assertContains(response, self.detail(self.batch, True))
        response = self.client.get(self.detail(self.batch, True))
        for value in ("workflow.css", "workflow-detail-grid", "detail-main-card", "detail-actions-card", "workflow-doc-grid",
                      "detail-info-grid", "workflow-price-heading", "workflow-price-summary", "simple-table-wrap"):
            self.assertContains(response, value)
        self.assertNotIn('method="post"', response.content.decode().split('<main', 1)[1].split('</main>', 1)[0])
        for value in ('value="generate"', 'value="validate"', 'reapply_prices', '/admin/', 'DELAHK', '84.00', '120.00'):
            self.assertNotContains(response, value)

    def test_b1_b2_strict_binding_missing_document_and_no_business_writes(self):
        confirmation = FactoryConfirmation.objects.create(order=self.order, factory=self.order.factory,
            shipping_date=date(2026, 5, 2), created_by=self.user, extraction_status="success")
        second = ShipmentBatch.objects.create(order=self.order, factory_confirmation=confirmation, batch_number=2,
            batch_date=confirmation.shipping_date, month_key="2026-05")
        ShipmentBatchItem.objects.create(batch=second, product=self.order_item.product, product_code=self.order_item.product_code, shipped_quantity=1)
        SerialItem.objects.create(order=self.order, factory_confirmation=confirmation, product=self.order_item.product,
            product_code=self.order_item.product_code, serial_number="B2-ONLY", expiration_date=date(2028, 1, 1))
        second_invoice = self.make_document("hospital_invoice", "INVOICE-B2", {}, batch=second)
        for operations in (False, True):
            for batch in (self.batch, second):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(self.detail(batch, operations))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["row"]["batch_id"], batch.pk)
                self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
                if batch == second:
                    self.assertContains(response, "未生成")
                    self.assertNotContains(response, "CPO-147891-B1")
                    self.assertNotContains(response, f"/documents/{self.invoice.pk}/")
                    self.assertContains(response, "B2-ONLY")
                    if operations:
                        self.assertContains(response, f"/documents/{second_invoice.pk}/pdf/")
                else:
                    self.assertNotContains(response, "B2-ONLY")
        for route in ("portal:commercial_purchase_detail", "portal:commercial_operations_detail"):
            self.assertEqual(self.client.get(reverse(route, args=[999999])).status_code, 404)
            self.assertEqual(self.client.post(reverse(route, args=[self.batch.pk])).status_code, 403)

    def test_finance_uses_original_dashboard_structure_without_cost_context(self):
        response = self.client.get(reverse("portal:commercial_finance"))
        for value in ("finance/css/settlement_dashboard.css", "finance/js/settlement_dashboard.js", "finance-header",
                      "finance-filter-panel", "kpi-grid", "kpi-card", "finance-grid-two", "finance-panel",
                      "finance-chart-wrap", "finance-table", "table-scroll", "order-panel", "finance-footer"):
            self.assertContains(response, value)
        self.assertEqual(len(response.context["kpi_cards"]), 10)
        self.assertContains(response, f'批次: {len(response.context["order_rows"])}')
        for key in ("cards", "kpi_cards", "groups", "order_rows", "chart_data", "monthly_rows", "hospital_rows", "product_rows"):
            dto = json.dumps(response.context[key], default=str)
            for value in ("DELAHK", "84.00", "120.00", "factory_po", "price_policy", "/admin/", "source_data"):
                self.assertNotIn(value, dto)
        self.assertNotContains(response, "commercial.css")

    def test_generator_uses_exact_original_template_147891_price_total_and_preserves_originals(self):
        self.batch_item.shipped_quantity = self.order_item.requested_quantity = 53
        self.batch_item.save()
        self.order_item.save()
        self.serial.raw_data = {"delivered_quantity": 53}
        self.serial.save()
        # Set the synthetic frozen authority to 53; live shipment changes alone
        # must never change the quantity used by Commercial rendering.
        payload = self.factory_po.source_data["po_data"]
        payload["items"][0].update(quantity_raw=53.0, quantity="53.00", batch_quantity="53")
        payload["totals"].update(total_units_raw=53.0, total_units="53")
        self.factory_po.save(update_fields=["source_data"])
        Path(self.factory_po.html_file.path).write_text(render_po_html(
            po_data=payload, template_path=Path(settings.BASE_DIR) / "templates/factory_purchase_order.html"), encoding="utf-8")
        original = GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get()
        old_files = [Path(self.factory_po.pdf_file.path), Path(self.factory_po.html_file.path)]
        old_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in old_files]
        snapshot = build_commercial_snapshot(self.batch)
        self.assertEqual(snapshot["unit_price"], "160.00")
        self.assertEqual(snapshot["total_amount"], "8480.00")
        output = Path(self.media.name) / "parity-render"
        output.mkdir()
        with patch("commercial_pos.services.generation_service.render_po_html", wraps=render_po_html) as render:
            render_commercial_files(snapshot, output)
        self.assertEqual(render.call_args.kwargs["template_path"], Path(settings.BASE_DIR) / "templates/factory_purchase_order.html")
        html = (output / "document.html").read_text()
        for value in ("po-header", "po-items-table", "total-section", "total-row units", "total-row amount", "#3a8d24", "8,480.00", "160.00", "DELAHK-PRIVATE"):
            self.assertIn(value, html)
        self.assertNotIn("84.00", html)
        self.assertNotIn("120.00", html)
        self.assertEqual(GeneratedDocument.objects.filter(pk=self.factory_po.pk).values().get(), original)
        self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for p in old_files], old_hashes)

    def test_new_detail_routes_keep_demo_security_precedence_and_anonymous_boundary(self):
        self.demo.is_staff = self.demo.is_superuser = True
        self.demo.save()
        self.assertEqual(self.client.get(self.detail(self.batch)).status_code, 200)
        self.assertEqual(self.client.get('/portal/factory/').status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:commercial_document_file', args=[self.factory_po.pk, 'pdf'])).status_code, 404)
        self.client.logout()
        for url in (self.detail(self.batch), self.detail(self.batch, True)):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_commercial_translations_and_finance_frozen_date_filters(self):
        for language, title, empty in (("en", "Data & Finance", "No data available"),
                                       ("fr", "Données & Finance", "Aucune donnée disponible")):
            with translation.override(language):
                self.assertEqual(translation.gettext("暂无数据"), empty)
                self.assertNotEqual(translation.gettext("数据与财务"), "数据与财务")
        response = self.client.get(reverse("portal:commercial_finance"), {"date_from": "2026-05-01"})
        self.assertEqual(response.context["cards"][0]["value"], 0)
        response = self.client.get(reverse("portal:commercial_finance"), {"date_from": "2026-04-22", "date_to": "2026-04-22"})
        self.assertEqual(response.context["cards"][3]["value"], "160.00 €")
        response = self.client.get(reverse("portal:commercial_finance"), {"date_from": "2026-13-01"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_errors"])

    def test_serial_with_conflicting_product_identity_is_not_exposed(self):
        self.serial.product = None
        self.serial.save()
        response = self.client.get(self.detail(self.batch))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["serial_rows"], [])
        self.assertNotContains(response, self.serial.serial_number)
