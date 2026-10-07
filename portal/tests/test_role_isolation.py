"""Opt-in account roles, direct URL boundaries and safe shared attachments."""
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from commercial_pos.services.generation_service import build_commercial_snapshot
from commercial_pos.tests.fixtures import FixtureMixin, make_factory_po
from commercial_pos.tests import test_showcase
from documents.models import GeneratedDocument
from hospitals.models import Hospital
from portal.role_access import BROWSE_PERMISSIONS, HOSPITAL_GROUP, INTERNAL_GROUP
from hospital_engagements.boss_access import is_boss_user


class PortalRoleIsolationTests(FixtureMixin, TestCase):
    make_document = test_showcase.ShowcaseSecurityTests.make_document

    def setUp(self):
        super().setUp()
        self.cynthia = get_user_model().objects.create_user(username="Cynthia", password="existing-test-password", is_staff=True,
                                                           email="kept@example.test", first_name="Existing")
        self.cynthia.user_permissions.add(Permission.objects.get(content_type__app_label="products", codename="change_product"))
        self.old_password = self.cynthia.password
        self.old_permissions = self.cynthia.get_all_permissions()
        call_command("configure_local_portal_roles", apply=True, stdout=StringIO())
        self.claire = get_user_model().objects.get(username="Claire")
        self.factory_po = make_factory_po(self.user, self.batch)
        self.cpo = self.make_document("commercial_po", "CPO-147891-B1", build_commercial_snapshot(self.batch))
        self.invoice = self.make_document("hospital_invoice", "Invoice-B1", {})
        self.hospital = Hospital.objects.create(name="Public hospital", notes="FACTORY-COST-SECRET")
        raw = Path(self.media.name) / self.order.hospital_order_pdf.name
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"%PDF-1.7\nHospital order")
        raw.with_suffix(".json").write_text('{"factory_cost":"FACTORY-COST-SECRET"}')
        self.client.force_login(self.claire)

    def modules(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("portal:home"))
        self.assertEqual(response.status_code, 200)
        return response, [(str(m["title"]), m["url"]) for m in response.context["modules"]]

    def test_claire_five_exact_modules_and_correct_urls_without_admin_rights(self):
        response, modules = self.modules(self.claire)
        self.assertEqual(modules, [("资料库", "/portal/library/"), ("医院订单", "/portal/orders/"),
                                  ("采购订单", "/portal/commercial/purchase-orders/"), ("操作平台", "/portal/commercial/operations/"),
                                  ("数据与财务", "/portal/commercial/finance/")])
        self.assertTrue(self.claire.is_active)
        self.assertFalse(self.claire.is_staff or self.claire.is_superuser or self.claire.has_usable_password())
        self.assertEqual(self.claire.get_all_permissions(), BROWSE_PERMISSIONS)
        self.assertNotContains(response, "/admin/")
        for title, url in modules:
            self.assertEqual(self.client.get(url).status_code, 200, title)

    def test_cynthia_six_exact_original_modules_and_permissions_preserved(self):
        response, modules = self.modules(self.cynthia)
        self.assertEqual(modules, [("资料库", "/portal/library/"), ("医院订单", "/portal/orders/"),
                                  ("工厂采购", "/portal/factory/"), ("工作流", "/portal/workflow/"),
                                  ("文档中心", "/portal/documents/"), ("发票与结算", "/portal/settlements/")])
        self.cynthia.refresh_from_db()
        self.assertEqual(self.cynthia.password, self.old_password)
        self.assertEqual(self.cynthia.get_all_permissions(), self.old_permissions)
        self.assertEqual(self.cynthia.email, "kept@example.test")
        self.assertTrue(self.cynthia.is_staff)
        for title, url in modules:
            self.assertEqual(self.client.get(url).status_code, 200, title)
        with patch("portal.views.run_order_extraction", return_value=("success", [], [])) as extract:
            response = self.client.post(reverse("portal:order_action", args=[self.order.pk]), {"action": "extract"})
        self.assertEqual(response.status_code, 302)
        extract.assert_called_once()

    def test_claire_direct_internal_routes_actions_admin_and_finance_are_denied(self):
        urls = ["/admin/", "/admin/login/", "/portal/factory/", "/portal/workflow/", "/portal/documents/", "/portal/settlements/",
                "/portal/finance/", "/portal/finance/export.xlsx", "/portal/library/prices/", "/portal/library/prices/simulator/",
                "/portal/shipments/", "/portal/backorders/", "/portal/orders/upload/", "/portal/library/hospitals/add/",
                "/portal/hospital-engagements/", "/portal/hospital-engagements/team-activity/",
                reverse("portal:factory_detail", args=[self.batch.factory_confirmation_id]),
                reverse("portal:workflow_detail", args=[self.workflow.pk]),
                reverse("portal:document_detail", args=[self.factory_po.pk]),
                reverse("portal:order_edit", args=[self.order.pk]), reverse("portal:order_action", args=[self.order.pk]),
                reverse("portal:order_factory_upload", args=[self.order.pk]),
                "/portal/settlements/1/payment/add/", "/portal/settlements/1/due-date/", "/portal/settlements/transactions/1/reverse/"]
        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url, {"action": "generate"}).status_code, 403)
        for route in ("portal:home", "portal:order_list", "portal:library_home", "portal:commercial_operations"):
            self.assertEqual(self.client.post(reverse(route)).status_code, 403)

    def test_cynthia_cannot_use_commercial_routes_even_with_explicit_permission(self):
        self.cynthia.user_permissions.add(Permission.objects.get(content_type__app_label="commercial_pos", codename="view_commercial_showcase"))
        self.client.force_login(self.cynthia)
        urls = [reverse("portal:" + route) for route in ("commercial_purchase_orders", "commercial_operations", "commercial_finance")]
        urls += [reverse("portal:" + route, args=[self.batch.pk]) for route in ("commercial_purchase_detail", "commercial_operations_detail", "commercial_batch")]
        urls += [reverse("portal:commercial_document_file", args=[self.cpo.pk, kind]) for kind in ("pdf", "html", "json")]
        for url in urls:
            for method in ("get", "post"):
                self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)
        self.cynthia.is_superuser = True
        self.cynthia.save(update_fields=["is_superuser"])
        self.assertEqual(self.client.get(urls[0]).status_code, 403)

    def test_cynthia_cannot_remove_isolation_via_account_group_or_session_admin(self):
        self.cynthia.user_permissions.add(*Permission.objects.filter(content_type__app_label="auth"))
        self.client.force_login(self.cynthia)
        for route, args in (("admin:auth_user_changelist", []), ("admin:auth_user_change", [self.cynthia.pk]),
                            ("admin:auth_group_changelist", []), ("admin:auth_group_change", [Group.objects.get(name=INTERNAL_GROUP).pk])):
            for method in ("get", "post"):
                self.assertEqual(getattr(self.client, method)(reverse(route, args=args)).status_code, 403)
        self.assertTrue(self.cynthia.groups.filter(name=INTERNAL_GROUP).exists())
        self.assertEqual(self.client.get("/portal/workflow/").status_code, 200)
        boss = get_user_model().objects.create_superuser(username="Acoeur", email="boss@example.test", password="test-only")
        self.client.force_login(boss)
        self.assertEqual(self.client.get(reverse("admin:auth_user_changelist")).status_code, 200)

    @override_settings(DEBUG=True)
    def test_pdf_json_raw_media_and_symlink_boundaries_for_both_roles(self):
        for document in (self.factory_po,):
            for kind in ("pdf", "html", "json"):
                self.assertEqual(self.client.get(reverse("portal:commercial_document_file", args=[document.pk, kind])).status_code, 404)
        for name in (self.factory_po.pdf_file.name, self.factory_po.html_file.name, self.cpo.pdf_file.name,
                     str(Path(self.order.hospital_order_pdf.name).with_suffix(".json")), "backups/database.json"):
            for prefix in ("/portal/files/", "/media/"):
                response = self.client.get(prefix + name)
                self.assertIn(response.status_code, (403, 404))
                self.assertNotIn("Traceback", response.content.decode())
        self.assertEqual(self.client.get("/outputs/" + self.factory_po.pdf_file.name).status_code, 404)
        for kind in ("pdf", "html"):
            response = self.client.get(reverse("portal:commercial_document_file", args=[self.cpo.pk, kind]))
            self.assertEqual(response.status_code, 200)
            response.close()
        for kind in ("pdf", "html"):
            response = self.client.get(reverse("portal:commercial_document_file", args=[self.invoice.pk, kind]))
            self.assertEqual(response.status_code, 200)
            response.close()
        self.assertEqual(self.client.get(reverse("portal:commercial_document_file", args=[self.invoice.pk, "json"])).status_code, 404)
        self.client.force_login(self.cynthia)
        for name in (self.cpo.pdf_file.name, self.cpo.html_file.name, "commercial_purchase_orders/old-revision/document.pdf"):
            for prefix in ("/portal/files/", "/media/"):
                self.assertIn(self.client.get(prefix + name).status_code, (403, 404))
        alias = Path(self.media.name) / "alias.pdf"
        alias.symlink_to(Path(self.cpo.pdf_file.path))
        self.assertEqual(self.client.get("/portal/files/alias.pdf").status_code, 403)
        self.assertEqual(self.client.get("/portal/files/./commercial_purchase_orders/../commercial_purchase_orders/old.pdf").status_code, 403)
        response = self.client.get("/portal/files/" + self.factory_po.pdf_file.name)
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get(reverse("portal:document_detail", args=[self.cpo.pk])).status_code, 404)

    def test_shared_library_and_order_details_never_receive_internal_context_or_links(self):
        routes = [("library_home", []), ("library_products", []), ("library_product_detail", [self.order_item.product_id]),
                  ("library_product_category", [self.order_item.product.category_id]),
                  ("library_product_factory", [self.order_item.product.category.parent_id]),
                  ("library_product_department", [self.order_item.product.category.parent.parent_id]),
                  ("library_hospitals", []), ("library_hospital_detail", [self.hospital.pk]),
                  ("library_factories", []), ("library_factory_detail", [self.order.factory_id]),
                  ("order_list", []), ("order_detail", [self.order.pk])]
        for route, args in routes:
            with self.subTest(route=route), CaptureQueriesContext(connection) as queries:
                response = self.client.get(reverse("portal:" + route, args=args))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                for forbidden in ("factory_unit_price", "price_policy", "source_data", "po_data", "FACTORY-COST-SECRET", "/portal/workflow/", "/portal/factory/", "/portal/settlements/", "/admin/", 'name="action"'):
                    self.assertNotIn(forbidden, html)
                    for key in ("rows", "fields", "modules", "attachments"):
                        self.assertNotIn(forbidden, json.dumps(response.context[key], default=str))
            self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        response = self.client.get(reverse("portal:order_detail", args=[self.order.pk]))
        self.assertContains(response, self.order.hospital_order_pdf.name)
        response = self.client.get("/portal/files/" + self.order.hospital_order_pdf.name)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7\nHospital order")
        response.close()

    def test_other_staff_admin_and_demo_modules_and_boss_rule_are_unchanged(self):
        for user, expected in ((self.user, ["资料库", "医院订单", "工厂采购", "工作流", "文档中心", "财务数据", "发票与结算"]),):
            response, modules = self.modules(user)
            self.assertEqual([title for title, url in modules], expected)
        boss = get_user_model().objects.create_superuser(username="Acoeur", email="boss@example.test", password="test-only")
        response, modules = self.modules(boss)
        self.assertTrue(is_boss_user(boss))
        self.assertIn("团队工作概览", [title for title, url in modules])
        self.assertEqual(len(modules), 12)
        viewer = get_user_model().objects.create_user(username="existing-demo")
        viewer.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0], Group.objects.get(name=HOSPITAL_GROUP))
        response, modules = self.modules(viewer)
        self.assertEqual([title for title, url in modules], ["采购订单", "操作平台", "数据与财务"])
        self.assertEqual(self.client.get("/portal/orders/").status_code, 403)
        self.assertEqual(self.client.get("/portal/library/").status_code, 403)
        for user in (self.claire, self.cynthia, self.user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("portal:team_activity")).status_code, 403)

    def test_home_subtitle_is_removed_without_changing_purchase_card_assets(self):
        admin = get_user_model().objects.create_superuser(username="other-admin", email="admin@example.test", password="test-only")
        response, _ = self.modules(admin)
        soup = BeautifulSoup(response.content, "html.parser")
        card = soup.select_one('a[href="/portal/commercial/purchase-orders/"]')
        self.assertEqual(card.select_one("h2").get_text(strip=True), "采购订单")
        self.assertFalse(card.select(".module-content > p"))
        self.assertIn("portal/img/app-icons/factory-purchase.png", card.select_one("img")["src"])
        self.assertIn("icon-green", card.select_one(".module-icon")["class"])
        self.assertNotContains(response, "只读查看商业展示采购订单")

    def test_claire_home_and_browsing_keep_all_three_languages(self):
        for language in ("zh-hans", "en", "fr"):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
            response = self.client.get("/portal/")
            with translation.override(language):
                self.assertEqual([str(m["title"]) for m in response.context["modules"]],
                                 [translation.gettext(t) for t in ("资料库", "医院订单", "采购订单", "操作平台", "数据与财务")])
            visible = BeautifulSoup(response.content, "html.parser").get_text().lower()
            for term in ("商业展示", "只读查看", "commercial", "demo"):
                self.assertNotIn(term, visible)
            for url in ("/portal/library/", "/portal/orders/", reverse("portal:order_detail", args=[self.order.pk])):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(reverse("set_language"), {"language": "en", "next": "/portal/"}).status_code, 302)
        self.assertEqual(self.client.post("/portal/logout/").status_code, 302)
        self.assertEqual(self.client.get("/portal/").status_code, 302)

    def test_conflicting_roles_fail_closed_and_missing_permissions_remove_card_and_access(self):
        permission = Permission.objects.get(content_type__app_label="orders", codename="view_order")
        Group.objects.get(name=HOSPITAL_GROUP).permissions.remove(permission)
        response = self.client.get("/portal/")
        self.assertNotIn("/portal/orders/", [m["url"] for m in response.context["modules"]])
        self.assertEqual(self.client.get("/portal/orders/").status_code, 403)
        self.claire.groups.add(Group.objects.get(name=INTERNAL_GROUP))
        self.assertEqual(self.client.get("/portal/").status_code, 403)
        self.assertEqual(self.client.get("/portal/commercial/purchase-orders/").status_code, 403)

    def test_local_command_is_idempotent_and_never_overwrites_existing_password_or_profile(self):
        self.claire.set_password("administrator-chosen-test-password")
        self.claire.email = "preserved@example.test"
        self.claire.save()
        before = get_user_model().objects.count(), self.claire.password, self.cynthia.password
        call_command("configure_local_portal_roles", apply=True, stdout=StringIO())
        self.claire.refresh_from_db(); self.cynthia.refresh_from_db()
        self.assertEqual((get_user_model().objects.count(), self.claire.password, self.cynthia.password), before)
        self.assertEqual(self.claire.email, "preserved@example.test")
        self.assertEqual(self.claire.groups.filter(name=HOSPITAL_GROUP).count(), 1)

    def test_local_command_rejects_incompatible_existing_identity_without_mutation(self):
        self.claire.is_staff = True
        self.claire.save()
        before = get_user_model().objects.count(), Group.objects.count()
        with self.assertRaises(CommandError):
            call_command("configure_local_portal_roles", apply=True, stdout=StringIO())
        self.assertEqual((get_user_model().objects.count(), Group.objects.count()), before)

    def test_provisioning_only_changes_auth_data_and_dry_run_does_not_create_users(self):
        self.claire.delete()
        before = {model: list(model.objects.order_by("pk").values()) for model in (GeneratedDocument, type(self.order), type(self.batch), type(self.order_item))}
        call_command("configure_local_portal_roles", stdout=StringIO())
        self.assertFalse(get_user_model().objects.filter(username="Claire").exists())
        call_command("configure_local_portal_roles", apply=True, stdout=StringIO())
        self.assertEqual({model: list(model.objects.order_by("pk").values()) for model in before}, before)

    @override_settings(SETTINGS_MODULE="config.settings_production")
    def test_provisioning_refuses_staging_production_settings(self):
        with self.assertRaises(CommandError):
            call_command("configure_local_portal_roles", apply=True, stdout=StringIO())
