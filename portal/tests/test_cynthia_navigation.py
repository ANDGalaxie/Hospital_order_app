"""Cynthia-only navigation, real engagement access and unchanged authorization."""
import json
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from commercial_pos.tests.fixtures import FixtureMixin, make_factory_po
from commercial_pos.tests import test_showcase
from commercial_pos.services.generation_service import build_commercial_snapshot
from documents.models import GeneratedDocument
from hospital_engagements.models import ACCESS_PERMISSION, HospitalEngagement
from hospitals.models import Hospital
from portal.role_access import BROWSE_PERMISSIONS, HOSPITAL_GROUP, INTERNAL_GROUP, portal_navigation


class CynthiaNavigationTests(FixtureMixin, TestCase):
    make_document = test_showcase.ShowcaseSecurityTests.make_document

    def setUp(self):
        super().setUp()
        self.cynthia = get_user_model().objects.create_user(username="Cynthia", is_staff=True, password="synthetic-only")
        self.cynthia.groups.add(Group.objects.get_or_create(name=INTERNAL_GROUP)[0])
        self.access = Permission.objects.get(content_type__app_label="hospital_engagements", codename="access_hospital_engagement")
        self.cynthia.user_permissions.add(self.access)
        self.permissions_before = self.cynthia.get_all_permissions()
        self.identity_before = (self.cynthia.password, self.cynthia.is_active, self.cynthia.is_staff, self.cynthia.is_superuser)
        self.hospital = Hospital.objects.create(name="Navigation Hospital")
        self.factory_po = make_factory_po(self.user, self.batch)
        self.po = self.make_document("commercial_po", "CPO-147891-B1", build_commercial_snapshot(self.batch))
        self.invoice = self.make_document("hospital_invoice", "Invoice Navigation", {})
        self.client.force_login(self.cynthia)

    def assert_no_admin_navigation(self, response):
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertFalse([a for a in soup.select("a[href]") if a["href"].startswith("/admin/")])
        self.assertNotIn("Admin", soup.get_text())

    def test_exact_seven_cards_correct_order_engagement_url_and_existing_card_style(self):
        response = self.client.get(reverse("portal:home"))
        soup = BeautifulSoup(response.content, "html.parser")
        cards = soup.select("a.module-card")
        self.assertEqual([card.select_one("h2").get_text(strip=True) for card in cards],
                         ["资料库", "医院订单", "医院沟通进度", "工厂采购", "工作流", "文档中心", "发票与结算"])
        self.assertEqual([card["href"] for card in cards], ["/portal/library/", "/portal/orders/", "/portal/hospital-engagements/",
                          "/portal/factory/", "/portal/workflow/", "/portal/documents/", "/portal/settlements/"])
        card = cards[2]
        self.assertIn("icon-teal", card.select_one(".module-icon")["class"])
        self.assertEqual(card.select_one("img")["src"], "/static/portal/img/app-icons/hospital-orders.png")
        self.assert_no_admin_navigation(response)

    def test_existing_access_permission_is_sufficient_for_home_list_detail_and_business_handler(self):
        self.assertEqual(ACCESS_PERMISSION, "hospital_engagements.access_hospital_engagement")
        for url in (reverse("portal:engagement_home"), reverse("portal:engagement_stage", args=["stage-1"]),
                    reverse("portal:engagement_detail", args=[self.hospital.pk])):
            self.assert_no_admin_navigation(self.client.get(url))
        with patch("portal.hospital_engagement_views.change_hospital_engagement_stage") as change:
            response = self.client.post(reverse("portal:engagement_change_stage", args=[self.hospital.pk]), {"stage": "stage_2"})
        self.assertEqual(response.status_code, 302)
        change.assert_called_once()
        self.assertEqual(self.hospital.engagement.stage, HospitalEngagement.Stage.FIRST)

    def test_missing_permission_hides_engagement_card_and_denies_direct_access(self):
        self.cynthia.user_permissions.remove(self.access)
        response = self.client.get(reverse("portal:home"))
        self.assertEqual(len(response.context["modules"]), 6)
        self.assertNotContains(response, 'href="/portal/hospital-engagements/"')
        self.assertEqual(self.client.get(reverse("portal:engagement_home")).status_code, 403)

    def test_admin_hidden_on_all_existing_shortcuts_and_representative_public_components(self):
        routes = [("home", []), ("library_home", []), ("library_products", []),
                  ("library_product_department", [self.order_item.product.category.parent.parent_id]),
                  ("library_product_factory", [self.order_item.product.category.parent_id]),
                  ("library_product_category", [self.order_item.product.category_id]),
                  ("library_product_detail", [self.order_item.product_id]), ("library_hospitals", []),
                  ("library_hospital_detail", [self.hospital.pk]), ("library_factories", []),
                  ("library_factory_detail", [self.order.factory_id]), ("library_prices", []), ("library_price_policy_simulator", []),
                  ("order_list", []), ("order_detail", [self.order.pk]), ("order_upload", []),
                  ("factory_list", []), ("factory_detail", [self.batch.factory_confirmation_id]),
                  ("workflow_list", []), ("workflow_detail", [self.workflow.pk]),
                  ("document_center", []), ("document_list", []), ("document_detail", [self.factory_po.pk]),
                  ("settlement_home", []), ("settlement_comparison", []), ("settlement_receivables", []),
                  ("settlement_payables", []), ("settlement_transactions", []), ("shipment_list", []), ("backorder_list", []),
                  ("engagement_home", []), ("engagement_stage", ["stage-1"]), ("engagement_detail", [self.hospital.pk])]
        for route, args in routes:
            with self.subTest(route=route):
                self.assert_no_admin_navigation(self.client.get(reverse("portal:" + route, args=args)))

    def test_original_six_modules_and_commercial_boundary_and_admin_backend_are_unchanged(self):
        for url in ("/portal/library/", "/portal/orders/", "/portal/factory/", "/portal/workflow/", "/portal/documents/", "/portal/settlements/"):
            self.assertEqual(self.client.get(url).status_code, 200)
        for route in ("commercial_purchase_orders", "commercial_operations", "commercial_finance"):
            self.assertEqual(self.client.get(reverse("portal:" + route)).status_code, 403)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:auth_user_changelist")).status_code, 403)
        self.cynthia.refresh_from_db()
        self.assertEqual(self.cynthia.get_all_permissions(), self.permissions_before)
        self.assertEqual((self.cynthia.password, self.cynthia.is_active, self.cynthia.is_staff, self.cynthia.is_superuser), self.identity_before)

    def test_admin_navigation_still_visible_for_acoeurs_and_other_members_of_same_group(self):
        boss = get_user_model().objects.create_superuser(username="Acoeurs", email="boss@example.test", password="synthetic-only")
        other = get_user_model().objects.create_user(username="another-internal", is_staff=True)
        other.groups.add(Group.objects.get(name=INTERNAL_GROUP))
        other.user_permissions.add(self.access)
        routes = [("home", []), ("order_list", []), ("order_detail", [self.order.pk]), ("factory_list", []),
                  ("workflow_list", []), ("workflow_detail", [self.workflow.pk]), ("library_product_detail", [self.order_item.product_id])]
        for user in (boss, other):
            self.client.force_login(user)
            for route, args in routes:
                response = self.client.get(reverse("portal:" + route, args=args))
                soup = BeautifulSoup(response.content, "html.parser")
                self.assertTrue([a for a in soup.select("a[href]") if a["href"].startswith("/admin/")], (user.username, route))
        response = self.client.get(reverse("portal:home"))
        self.assertEqual(len(response.context["modules"]), 6)
        self.assertTrue(portal_navigation(other)["show_admin"])
        self.client.force_login(boss)
        self.assertContains(self.client.get(reverse("portal:home")), 'href="/portal/hospital-engagements/team-activity/"')

    def test_boss_display_uses_exact_username_despite_profile_and_child_role_override(self):
        from hospital_engagements.boss_access import is_boss_user
        from portal.services.common import get_user_display_name
        boss = get_user_model().objects.create_superuser(username="Acoeurs", first_name="Acoeur",
                                                         email="boss@example.test", password="synthetic-only")
        self.assertEqual(get_user_display_name(boss), "Acoeurs")
        ordinary = get_user_model()(username="ordinary-name", first_name="Acoeurs")
        self.assertFalse(is_boss_user(ordinary))
        self.assertEqual(get_user_display_name(ordinary), "Acoeurs")
        self.client.force_login(boss)
        for language, role in (("zh-hans", "负责人 · Acoeurs"), ("en", "Owner · Acoeurs"), ("fr", "Responsable · Acoeurs")):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
            for path in (reverse("portal:home"), reverse("portal:team_activity"),
                         reverse("portal:commercial_operations_detail", args=[self.batch.pk])):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.content, "html.parser")
                self.assertEqual(soup.select_one(".user-name").get_text(strip=True), "Acoeurs")
                self.assertEqual(soup.select_one(".user-role").get_text(strip=True), role)
                self.assertEqual(soup.select_one(".brand-logo-img")["alt"], "Acoeur")
                self.assertTrue(soup.select('form[action="/portal/logout/"]'))
                if path == reverse("portal:home"):
                    self.assertTrue(soup.select('a[href="/admin/"]'))
                    self.assertTrue(soup.select('a[href="/portal/hospital-engagements/team-activity/"]'))

    def test_claire_five_cards_and_demo_isolation_unchanged(self):
        claire = get_user_model().objects.create_user(username="Claire")
        hospital_group = Group.objects.get_or_create(name=HOSPITAL_GROUP)[0]
        for permission in Permission.objects.select_related("content_type"):
            if f"{permission.content_type.app_label}.{permission.codename}" in BROWSE_PERMISSIONS:
                hospital_group.permissions.add(permission)
        claire.groups.add(hospital_group)
        self.client.force_login(claire)
        response = self.client.get(reverse("portal:home"))
        self.assertEqual([str(m["title"]) for m in response.context["modules"]], ["资料库", "医院订单", "采购订单", "操作平台", "数据与财务"])
        self.assertEqual(self.client.get(reverse("portal:engagement_home")).status_code, 403)
        demo = get_user_model().objects.create_user(username="existing-hospital-demo", is_staff=True, is_superuser=True)
        demo.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        self.client.force_login(demo)
        response = self.client.get(reverse("portal:home"))
        self.assertEqual([str(m["title"]) for m in response.context["modules"]], ["采购订单", "操作平台", "数据与财务"])
        self.assertNotContains(response, "/admin/")
        self.assertEqual(self.client.get(reverse("portal:engagement_home")).status_code, 403)
        self.assertEqual(self.client.get("/portal/workflow/").status_code, 403)

    def test_three_languages_hide_admin_and_use_existing_engagement_translation(self):
        for language in ("zh-hans", "en", "fr"):
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
            response = self.client.get(reverse("portal:home"))
            self.assert_no_admin_navigation(response)
            soup = BeautifulSoup(response.content, "html.parser")
            card = soup.select_one('a[href="/portal/hospital-engagements/"]')
            with translation.override(language):
                self.assertEqual(card.select_one("h2").get_text(strip=True), translation.gettext("医院沟通进度"))
            self.assertEqual(len(soup.select("a.module-card")), 7)

    def test_get_pages_do_not_write_business_or_authorization_data(self):
        for url in ("/portal/", "/portal/hospital-engagements/", reverse("portal:engagement_stage", args=["stage-1"]),
                    reverse("portal:engagement_detail", args=[self.hospital.pk]), "/portal/orders/", "/portal/workflow/"):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200)
            self.assertFalse([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
