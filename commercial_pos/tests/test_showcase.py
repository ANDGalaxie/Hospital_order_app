import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.generation_service import build_commercial_snapshot, render_commercial_files
from documents.models import GeneratedDocument
from factory_confirmations.models import FactoryConfirmation, SerialItem
from portal.services.commercial_showcase_service import build_commercial_finance_context, build_showcase_list_context
from portal.services.common import get_global_numeric_bon_ordinals
from shipments.models import ShipmentBatch, ShipmentBatchItem

from .fixtures import FixtureMixin, fake_render, make_factory_po


class ShowcaseSecurityTests(FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.demo = get_user_model().objects.create_user(username="arbitrary-display-user", password="test")
        self.demo.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(self.demo)
        self.factory_po = make_factory_po(self.user, self.batch, number="DELAHK-SENSITIVE")
        self.cpo = self.make_document("commercial_po", "CPO-147891-B1", build_commercial_snapshot(self.batch))
        self.invoice = self.make_document("hospital_invoice", "Invoice Synthetic B1", {"invoice_data": {"totals": {"total_raw": "270.00"}}})
        self.factory_po.source_data["notes"] = "private-cost-sentinel"
        self.factory_po.save(update_fields=["source_data"])
        self.pages = [reverse("portal:commercial_purchase_orders"), reverse("portal:commercial_operations"), reverse("portal:commercial_finance")]

    def make_document(self, kind, number, source, *, batch=None):
        batch = batch or self.batch
        path = Path(self.media.name) / "synthetic" / number.replace(" ", "_")
        fake_render({"document_number": number}, path)
        return GeneratedDocument.objects.create(order=batch.order, shipment_batch=batch, document_type=kind,
                                                document_number=number, generated_by=self.user, source_data=source,
                                                pdf_file=str((path / "document.pdf").relative_to(self.media.name)),
                                                html_file=str((path / "document.html").relative_to(self.media.name)))

    def file_url(self, doc, kind="pdf"):
        return reverse("portal:commercial_document_file", args=[doc.pk, kind])

    def test_demo_home_has_only_three_cards(self):
        response = self.client.get(reverse("portal:home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["title"] for m in response.context["modules"]], ["采购订单", "操作平台", "数据与财务"])
        for fragment in ("/admin/", "/portal/factory/", "/portal/workflow/", "/portal/settlements/", "DELAHK"):
            self.assertNotContains(response, fragment)

    def test_demo_denied_every_internal_route_and_post_even_if_superuser_staff(self):
        self.demo.is_staff = self.demo.is_superuser = True
        self.demo.save()
        self.demo.groups.add(Group.objects.get_or_create(name="Internal staff")[0])
        urls = ["/admin/", "/admin/login/", "/portal/workflow/", "/portal/finance/", "/portal/finance/export.xlsx",
                "/portal/settlements/", "/portal/orders/", "/portal/factory/", "/portal/library/", "/portal/documents/",
                "/portal/hospital-engagements/", "/portal/hospital-engagements/team-activity/",
                f"/portal/workflow/{self.workflow.pk}/action/", f"/portal/orders/{self.order.pk}/action/",
                f"/portal/factory/{self.batch.factory_confirmation_id}/action/"]
        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url, {"action": "generate"}).status_code, 403)
        for url in [reverse("portal:home"), *self.pages, self.file_url(self.cpo)]:
            self.assertEqual(self.client.post(url).status_code, 403)

    def test_allowed_files_stream_pdf_html_download_but_reject_factory(self):
        for document in (self.cpo, self.invoice):
            for kind in ("pdf", "html"):
                for suffix in ("", "?download=1"):
                    with self.subTest(kind=kind, suffix=suffix):
                        response = self.client.get(self.file_url(document, kind) + suffix)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response["Cache-Control"], "private, no-store")
                        self.assertIn("Cookie", response["Vary"])
                        self.assertIn("sandbox", response["Content-Security-Policy"])
                        self.assertTrue(b"".join(response.streaming_content))
        for kind in ("pdf", "html", "source_data", "json"):
            response = self.client.get(self.file_url(self.factory_po, kind))
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "private-cost-sentinel", status_code=404)

    @override_settings(DEBUG=True)
    def test_raw_media_outputs_and_attachment_paths_cannot_bypass(self):
        for relative in (self.factory_po.pdf_file.name, self.factory_po.html_file.name,
                         "factory_confirmations/synthetic.pdf", "hospital_orders/synthetic.pdf", "backups/backup.zip"):
            for prefix in ("/media/", "/portal/files/", "/outputs/"):
                response = self.client.get(prefix + relative)
                self.assertIn(response.status_code, (403, 404))
                self.assertNotIn(str(Path(self.media.name)), response.content.decode())
        response = self.client.get(self.file_url(self.cpo, "json"))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Traceback", status_code=404)

    def test_anonymous_non_staff_and_internal_permission_boundary(self):
        self.client.logout()
        for url in [*self.pages, self.file_url(self.cpo), self.file_url(self.invoice, "html")]:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        for url in self.pages:
            self.assertEqual(self.client.get(url).status_code, 403)
        permission = Permission.objects.get(content_type__app_label="commercial_pos", codename="view_commercial_showcase")
        self.user.user_permissions.add(permission)
        for url in self.pages:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get("/portal/workflow/").status_code, 200)
        response = self.client.get("/portal/")
        self.assertTrue(any(m["title"] == "工厂采购" for m in response.context["modules"]))

    def test_login_logout_language_and_inactive_account(self):
        self.client.logout()
        response = self.client.post("/portal/login/", {"username": self.demo.username, "password": "test", "next": "/admin/"})
        self.assertRedirects(response, "/portal/")
        for code, title in (("en", "Purchase Orders"), ("fr", "Commandes d’achat"), ("zh-hans", "采购订单")):
            response = self.client.post(reverse("set_language"), {"language": code, "next": self.pages[0]})
            self.assertEqual(response.status_code, 302)
            self.assertContains(self.client.get(self.pages[0]), title)
        self.assertRedirects(self.client.post("/portal/logout/"), "/portal/login/")
        self.assertEqual(self.client.get(self.file_url(self.cpo)).status_code, 302)
        self.demo.is_active = False
        self.demo.save()
        self.assertContains(self.client.post("/portal/login/", {"username": self.demo.username, "password": "test"}), "errorlist")

    def test_readonly_gets_have_no_business_writes_and_no_cost_context(self):
        for url in ["/portal/", *self.pages, reverse("portal:commercial_batch", args=[self.batch.pk])]:
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
            for text in ("DELAHK-SENSITIVE", "private-cost-sentinel", self.factory_po.pdf_file.name, "84.00", "120.00", "price_policy_id", "validation_data", "/admin/"):
                self.assertNotContains(response, text)
            if "page_obj" in response.context:
                self.assertNotIn("DELAHK", json.dumps(response.context["page_obj"].object_list, default=str))

    def test_batch_links_do_not_fallback_to_another_batch(self):
        other = ShipmentBatch.objects.create(order=self.order, source_type="manual", batch_number=2, batch_date=date(2026, 5, 2), month_key="2026-05")
        response = self.client.get(reverse("portal:commercial_batch", args=[other.pk]) + "?module=operations")
        row = response.context["row"]
        self.assertFalse(row["invoice"]["ready"])
        self.assertFalse(row["commercial_po"]["ready"])
        self.assertEqual(row["ordinal"], get_global_numeric_bon_ordinals()[self.order.pk])
        self.assertNotContains(response, self.file_url(self.invoice))
        purchases = self.client.get(self.pages[0])
        self.assertNotContains(purchases, self.file_url(self.invoice))

    def test_conflicting_invoice_snapshot_cannot_be_served(self):
        self.invoice.source_data = {"shipment_batch_id": self.batch.pk + 99}
        self.invoice.save()
        self.assertEqual(self.client.get(self.file_url(self.invoice)).status_code, 404)
        response = self.client.get(reverse("portal:commercial_batch", args=[self.batch.pk]) + "?module=operations")
        self.assertFalse(response.context["row"]["invoice"]["ready"])

    def test_purchase_detail_context_and_links_only_include_commercial_po(self):
        response = self.client.get(reverse("portal:commercial_batch", args=[self.batch.pk]))
        self.assertNotIn("invoice", response.context["row"])
        self.assertNotContains(response, "Hospital Invoice")
        self.assertNotContains(response, self.file_url(self.invoice))

    def test_actual_debug_media_route_is_authenticated_and_demo_denied(self):
        import importlib.util
        import sys
        from unittest.mock import patch
        from django.conf import settings
        name = "config._commercial_acceptance_debug_urls"
        with override_settings(DEBUG=True):
            spec = importlib.util.spec_from_file_location(name, Path(settings.BASE_DIR) / "config/urls.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with patch.dict(sys.modules, {name: module}), override_settings(ROOT_URLCONF=name):
                url = "/media/" + self.factory_po.pdf_file.name
                self.demo.is_staff = True
                self.demo.save()
                self.assertEqual(self.client.get(url).status_code, 403)
                self.client.force_login(self.user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(b"".join(response.streaming_content))
                self.client.logout()
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_two_batch_statistics_deduplicate_bon_and_never_double_count_batch(self):
        confirmation = FactoryConfirmation.objects.create(order=self.order, factory=self.order.factory,
            shipping_date=date(2026, 5, 2), created_by=self.user, extraction_status="success")
        batch = ShipmentBatch.objects.create(order=self.order, factory_confirmation=confirmation,
            batch_date=confirmation.shipping_date, batch_number=2, month_key="2026-05")
        ShipmentBatchItem.objects.create(batch=batch, product=self.order_item.product, product_code=self.order_item.product_code, shipped_quantity=1)
        SerialItem.objects.create(order=self.order, factory_confirmation=confirmation, product=self.order_item.product,
            product_code=self.order_item.product_code, serial_number="SYN-B2", expiration_date=date(2028, 1, 1))
        make_factory_po(self.user, batch)
        self.make_document("commercial_po", "CPO-147891-B2", build_commercial_snapshot(batch), batch=batch)
        response = self.client.get(self.pages[2])
        cards = response.context["cards"]
        self.assertEqual([card["value"] for card in cards], [2, 1, "2", "320.00 €"])
        self.assertEqual(len(response.context["groups"][0]["rows"]), 2)
        self.cpo.source_data.pop("price_policy_id")
        self.cpo.save()
        self.assertEqual(self.client.get(self.pages[2]).context["ignored_count"], 1)

    def test_broken_snapshot_excluded_and_history_not_repriced(self):
        factory = RequestFactory()
        request = factory.get(self.pages[2])
        request.user = self.demo
        original = build_commercial_finance_context(request)
        self.assertEqual(original["cards"][3]["value"], "160.00 €")
        self.assertEqual(original["groups"][0]["rows"][0]["label"], "2026-04")
        CommercialPOPricePolicy.objects.update(unit_price=Decimal("199.00"))
        self.assertEqual(build_commercial_finance_context(request), original)
        self.cpo.source_data = {"total_amount": "160.00"}
        self.cpo.save()
        broken = build_commercial_finance_context(request)
        self.assertEqual(broken["ignored_count"], 1)
        self.assertEqual(broken["cards"][3]["value"], "0.00 €")
        self.assertContains(self.client.get(self.pages[2]), "存在未计入记录")

    def test_pagination_retains_filters_and_safe_dtos(self):
        for i in range(2, 29):
            ShipmentBatch.objects.create(order=self.order, source_type="manual", batch_number=i, month_key="2026-04")
        response = self.client.get(self.pages[0], {"q": "147891", "status": "missing", "page": 2})
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(response.context["query_without_page"], "q=147891&status=missing")
        self.assertEqual(len(response.context["page_obj"].object_list), 2)

    def test_role_change_logout_and_no_shared_cache(self):
        self.client.force_login(self.user)
        response = self.client.get("/portal/workflow/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.user.groups.add(Group.objects.get(name="Hospital Demo"))
        self.assertEqual(self.client.get("/portal/workflow/").status_code, 403)
        self.assertNotContains(self.client.get("/portal/"), "/admin/")
        self.client.post("/portal/logout/")
        self.assertEqual(self.client.get(self.file_url(self.invoice)).status_code, 302)

    def test_file_path_escape_and_batch_order_mismatch_rejected(self):
        outside = Path(self.media.name).parent / (Path(self.media.name).name + "-outside.pdf")
        outside.write_bytes(b"%PDF-secret")
        self.addCleanup(outside.unlink, missing_ok=True)
        GeneratedDocument.objects.filter(pk=self.invoice.pk).update(pdf_file="../" + outside.name)
        self.assertEqual(self.client.get(self.file_url(self.invoice)).status_code, 404)
        wrong = ShipmentBatch.objects.create(order=self.order, batch_number=3, source_type="manual", month_key="2026-04")
        # A CPO snapshot frozen for B1 must not be served after relinking it to B3.
        GeneratedDocument.objects.filter(pk=self.cpo.pk).update(shipment_batch=wrong)
        self.assertEqual(self.client.get(self.file_url(self.cpo)).status_code, 404)

    def test_old_workflow_and_document_center_keep_two_document_types(self):
        self.workflow.invoice_document = self.invoice
        self.workflow.po_document = self.factory_po
        self.workflow.invoice_status = self.workflow.po_status = self.workflow.workflow_status = "generated"
        self.workflow.save()
        self.client.force_login(self.user)
        workflow = self.client.get("/portal/workflow/")
        self.assertNotContains(workflow, "Commercial PO")
        self.assertNotContains(workflow, self.file_url(self.cpo))
        center = self.client.get("/portal/documents/all/")
        self.assertEqual(center.context["total_count"], 2)
        self.assertNotContains(center, "commercial_po")

    def test_commercial_file_is_english_and_green_and_inherits_factory_identity(self):
        directory = Path(self.media.name) / "real-render"
        directory.mkdir()
        with translation.override("fr"):
            render_commercial_files(build_commercial_snapshot(self.batch), directory)
        html = (directory / "document.html").read_text()
        self.assertIn('lang="en"', html)
        self.assertIn("DELAHK-SENSITIVE", html)
        self.assertNotIn("Commercial Purchase Order", html)
        self.assertIn("#3a8d24", html)
        self.assertIn("Total Units", html)
        self.assertNotIn("IBAN", html)
        self.assertNotIn("120.00", html)
        self.assertTrue((directory / "document.pdf").read_bytes().startswith(b"%PDF-"))
