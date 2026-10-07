"""Shared logout visibility, CSRF protection and actual session revocation."""
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.urls import reverse
from portal.role_access import BROWSE_PERMISSIONS, HOSPITAL_GROUP, INTERNAL_GROUP


class PortalLogoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.cynthia = User.objects.create_user(username="Cynthia", is_staff=True)
        cls.cynthia.groups.add(Group.objects.get_or_create(name=INTERNAL_GROUP)[0])
        cls.cynthia.user_permissions.add(Permission.objects.get(content_type__app_label="hospital_engagements", codename="access_hospital_engagement"))
        cls.claire = User.objects.create_user(username="Claire")
        hospital = Group.objects.get_or_create(name=HOSPITAL_GROUP)[0]
        hospital.permissions.add(*[p for p in Permission.objects.select_related("content_type")
                                  if f"{p.content_type.app_label}.{p.codename}" in BROWSE_PERMISSIONS])
        cls.claire.groups.add(hospital)
        cls.boss = User.objects.create_user(username="Acoeur", is_staff=True, is_superuser=True)
        cls.staff = User.objects.create_user(username="ordinary-staff", is_staff=True)
        cls.demo = User.objects.create_user(username="hospital-demo")
        cls.demo.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])

    def logged_page(self, user, path="/portal/"):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        forms = soup.select('form[action="/portal/logout/"]')
        self.assertEqual(len(forms), 1)
        return client, response, soup, forms[0]

    def test_cynthia_logout_visible_admin_hidden_and_seven_cards_unchanged(self):
        client, response, soup, form = self.logged_page(self.cynthia)
        self.assertEqual(form["method"].lower(), "post")
        self.assertEqual(form.select_one('button[type="submit"]').get_text(strip=True), "Déconnexion")
        self.assertEqual(form.select_one("button")["class"], ["portal-button"])
        self.assertIsNotNone(form.select_one('input[name="csrfmiddlewaretoken"]'))
        self.assertFalse(soup.select('a[href^="/admin/"]'))
        self.assertEqual(len(soup.select("a.module-card")), 7)
        self.assertEqual([card["href"] for card in soup.select("a.module-card")], ["/portal/library/", "/portal/orders/", "/portal/hospital-engagements/",
                         "/portal/factory/", "/portal/workflow/", "/portal/documents/", "/portal/settlements/"])
        self.assertEqual(client.get("/admin/").status_code, 200)
        for path in ("/portal/orders/", "/portal/workflow/", "/portal/hospital-engagements/"):
            self.logged_page(self.cynthia, path)

    def test_same_shared_logout_markup_and_location_for_cynthia_and_claire(self):
        forms = []
        for user in (self.cynthia, self.claire):
            client, response, soup, form = self.logged_page(user)
            self.assertIn("user-area", form.parent.get("class", []))
            form.select_one('input[name="csrfmiddlewaretoken"]')["value"] = "TOKEN"
            forms.append(str(form))
        self.assertEqual(forms[0], forms[1])
        self.assertEqual(len(soup.select("a.module-card")), 5)

    def test_post_logout_with_rendered_csrf_flushes_session_and_blocks_protected_pages(self):
        for user, paths in ((self.cynthia, ("/portal/", "/portal/orders/", "/portal/workflow/", "/portal/hospital-engagements/")),
                            (self.claire, ("/portal/", "/portal/commercial/purchase-orders/", "/portal/commercial/operations/")),
                            (self.boss, ("/portal/", "/admin/")), (self.staff, ("/portal/", "/portal/orders/")),
                            (self.demo, ("/portal/", "/portal/commercial/operations/"))):
            with self.subTest(user=user.username):
                client, response, soup, form = self.logged_page(user)
                old_session_key = client.session.session_key
                self.assertIn(SESSION_KEY, client.session)
                result = client.post(form["action"], {"csrfmiddlewaretoken": form.select_one('input[name="csrfmiddlewaretoken"]')["value"]})
                self.assertEqual(result.status_code, 302)
                self.assertEqual(result.url, reverse("portal:showcase_login"))
                self.assertNotIn(SESSION_KEY, client.session)
                self.assertFalse(Session.objects.filter(session_key=old_session_key).exists())
                for path in paths:
                    self.assertEqual(client.get(path).status_code, 302, path)
                replay = Client()
                replay.cookies[settings.SESSION_COOKIE_NAME] = old_session_key
                self.assertEqual(replay.get("/portal/").status_code, 302)

    def test_csrf_required_and_get_does_not_logout(self):
        client, response, soup, form = self.logged_page(self.cynthia)
        session_key = client.session.session_key
        self.assertEqual(client.post(form["action"]).status_code, 403)
        self.assertEqual(client.get(form["action"]).status_code, 405)
        self.assertEqual(client.session.session_key, session_key)
        self.assertIn(SESSION_KEY, client.session)
        self.assertEqual(client.get("/portal/").status_code, 200)

    def test_acoeur_admin_and_existing_logout_routes_remain_available(self):
        for path in ("/portal/", "/portal/commercial/operations/"):
            client, response, soup, form = self.logged_page(self.boss, path)
            self.assertEqual(form["action"], reverse("portal:showcase_logout"))
            self.assertEqual(form["method"], "post")
            if path == "/portal/":
                self.assertTrue(soup.select('a[href="/admin/"]'))
            self.assertEqual(client.get("/admin/").status_code, 200)

    def test_anonymous_login_page_has_no_logout_form(self):
        response = self.client.get(reverse("portal:showcase_login"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BeautifulSoup(response.content, "html.parser").select('form[action="/portal/logout/"]'))
