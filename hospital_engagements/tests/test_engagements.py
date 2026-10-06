from datetime import date, timedelta
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from factories.models import Factory
from hospitals.models import Hospital
from orders.models import Order
from products.models import Product
from hospital_engagements.models import (
    ACCESS_PERMISSION, HospitalContact, HospitalDepartment, HospitalEngagement,
    HospitalFollowUp, HospitalProductInterest,
)
from hospital_engagements.services import add_communication, change_hospital_engagement_stage, save_contact
from portal.forms.hospital_engagement_forms import BusinessForm, ContactForm, DepartmentForm, InterestForm
from portal.services.hospital_engagement_service import decorate_rows, stage_queryset

User = get_user_model()


class EngagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("engagement-staff", is_staff=True)
        cls.permission = Permission.objects.get(
            content_type__app_label="hospital_engagements", codename="access_hospital_engagement"
        )
        cls.user.user_permissions.add(cls.permission)
        cls.other = User.objects.create_user("unprivileged-staff", is_staff=True)
        cls.hospital = Hospital.objects.create(
            name="Clinique Étoile", billing_address="Billing 原文",
            default_shipping_address="Shipping 原文", contact_name="Public contact", fax="FAX-123",
        )
        cls.second = Hospital.objects.create(name="Second hospital")
        cls.engagement = cls.hospital.engagement
        cls.department = HospitalDepartment.objects.create(hospital=cls.hospital, name="Cardiology")
        cls.contact = HospitalContact.objects.create(
            hospital=cls.hospital, department=cls.department, name="Dr Martin", contact_type="doctor"
        )
        cls.factory = Factory.objects.create(name="Engagement factory")
        cls.product = Product.objects.create(code="BMA-2.5015", factory=cls.factory)

    def setUp(self):
        self.client.force_login(self.user)
        self.addCleanup(translation.deactivate)

    def url(self, name, *args):
        return reverse("portal:engagement_" + name, args=args)

    def stage(self, **params):
        return self.client.get(self.url("stage", "stage-1"), params)

    def communication(self, **kwargs):
        data = dict(engagement=self.engagement, channel="phone", summary="Business 原文")
        data.update(kwargs)
        return add_communication(HospitalFollowUp(**data), self.user)

    def followup_data(self, **kwargs):
        data = dict(occurred_at="2026-10-05T14:00", channel="email", summary="Discussion 原文")
        data.update(kwargs)
        return data

    def record_url(self, kind, record=None, action="add"):
        args = [self.hospital.pk, kind]
        if record is not None:
            args.append(record.pk)
        return self.url("record_" + action, *args)

    def mutation_urls(self):
        return [
            self.url("change_stage", self.hospital.pk),
            self.url("bulk_move", "stage-1"),
            self.url("business", self.hospital.pk),
            self.url("follow_up", self.hospital.pk),
            self.url("primary", self.hospital.pk, self.contact.pk),
            self.record_url("departments"),
            self.record_url("contacts"),
            self.record_url("interests"),
            self.record_url("contacts", self.contact, "edit"),
            self.record_url("departments", self.department, "edit"),
            self.record_url("contacts", self.contact, "remove"),
            self.record_url("departments", self.department, "remove"),
        ]

    def test_authorized_home_card_and_position(self):
        response = self.client.get(reverse("portal:home"))
        modules = response.context["modules"]
        self.assertEqual(modules[1]["url"], self.url("home"))
        self.assertEqual(modules[1]["badge"], 2)

    def test_unprivileged_home_unchanged(self):
        with translation.override("en"):
            privileged = self.client.get(reverse("portal:home")).context["modules"]
            self.client.force_login(self.other)
            ordinary = self.client.get(reverse("portal:home")).context["modules"]
        self.assertEqual([m for m in privileged if m["url"] != self.url("home")], ordinary)
        self.assertEqual(len(ordinary), 7)

    def test_all_get_views_forbid_unprivileged_staff(self):
        self.client.force_login(self.other)
        urls = [self.url("home"), self.url("stage", "stage-1"), self.url("detail", self.hospital.pk),
                self.record_url("contacts"), self.record_url("contacts", self.contact, "edit")]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_all_mutations_forbid_unprivileged_staff(self):
        interest = HospitalProductInterest.objects.create(engagement=self.engagement, product_text="DES")
        self.client.force_login(self.other)
        urls = self.mutation_urls() + [self.record_url("interests", interest, "edit"),
                                     self.record_url("interests", interest, "remove")]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertFalse(HospitalFollowUp.objects.exists())

    def test_direct_user_permission(self):
        self.assertTrue(User.objects.get(pk=self.user.pk).has_perm(ACCESS_PERMISSION))
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)

    def test_group_permission(self):
        group = Group.objects.create(name="Hospital Engagement")
        group.permissions.add(self.permission)
        self.other.groups.add(group)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)

    def test_superuser_native_permission(self):
        admin = User.objects.create_superuser("engagement-admin", "admin@example.com", "test")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)

    def test_owner_does_not_grant_permission(self):
        self.engagement.owner = self.other
        self.engagement.save()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url("detail", self.hospital.pk)).status_code, 403)

    def test_permission_without_staff_is_insufficient(self):
        self.user.is_staff = False
        self.user.save()
        self.assertEqual(self.client.get(self.url("home")).status_code, 302)

    def test_inactive_staff_cannot_access(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.client.get(self.url("home")).status_code, 302)

    def test_anonymous_redirects(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url("home")).status_code, 302)

    def test_mutations_are_post_only(self):
        urls = self.mutation_urls()
        for url in urls[:5] + urls[-2:]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for url in self.mutation_urls():
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {}).status_code, 403)

    def test_signal_default_engagement(self):
        hospital = Hospital.objects.create(name="Future")
        self.assertEqual((hospital.engagement.stage, hospital.engagement.priority, hospital.engagement.owner_id),
                         ("stage_1", "B", None))

    def test_signal_idempotent_no_get_writes(self):
        self.hospital.save()
        with CaptureQueriesContext(connection) as captured:
            self.client.get(self.url("detail", self.hospital.pk))
        self.assertEqual(HospitalEngagement.objects.filter(hospital=self.hospital).count(), 1)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                             for q in captured))

    def test_missing_engagement_is_not_created_on_get(self):
        self.engagement.delete()
        self.assertEqual(self.client.get(self.url("detail", self.hospital.pk)).status_code, 404)
        self.assertFalse(HospitalEngagement.objects.filter(hospital=self.hospital).exists())

    def test_order_does_not_advance_stage(self):
        Order.objects.create(bon_de_commande="ENG-HISTORY", hospital=self.hospital, created_by=self.user)
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.stage, "stage_1")

    def test_inactive_hospital_history_retained_and_excluded(self):
        entry = self.communication()
        self.hospital.is_active = False
        self.hospital.save()
        self.assertEqual(self.stage().context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(self.url("home")).context["cards"][0]["count"], 1)
        self.assertTrue(HospitalFollowUp.objects.filter(pk=entry.pk).exists())
        self.hospital.is_active = True
        self.hospital.save()
        self.assertEqual(self.stage().context["page_obj"].paginator.count, 2)

    def test_stage_counts_live(self):
        change_hospital_engagement_stage(self.engagement, "stage_2", self.user)
        cards = self.client.get(self.url("home")).context["cards"]
        self.assertEqual([card["count"] for card in cards], [1, 1, 0])

    def test_stage_service_updates_and_history(self):
        change_hospital_engagement_stage(self.engagement, "stage_2", self.user)
        self.engagement.refresh_from_db()
        entry = self.engagement.follow_ups.get()
        self.assertEqual(self.engagement.stage, "stage_2")
        self.assertEqual((entry.activity_type, entry.stage_from, entry.stage_to, entry.created_by),
                         ("stage_change", "stage_1", "stage_2", self.user))
        self.assertEqual(entry.summary, "")

    def test_stage_noop_has_no_history(self):
        change_hospital_engagement_stage(self.engagement, "stage_1", self.user)
        self.assertFalse(self.engagement.follow_ups.exists())

    def test_stage_invalid_service_and_post(self):
        with self.assertRaises(ValidationError):
            change_hospital_engagement_stage(self.engagement, "stage_99", self.user)
        self.assertEqual(self.client.post(self.url("change_stage", self.hospital.pk),
                                         {"stage": "stage_99"}).status_code, 400)
        self.assertFalse(self.engagement.follow_ups.exists())

    def test_invalid_stage_slug_404(self):
        self.assertEqual(self.client.get(self.url("stage", "stage-99")).status_code, 404)
        self.assertEqual(self.client.post(self.url("bulk_move", "stage-99")).status_code, 404)

    def test_stage_post_preserves_safe_next(self):
        path = self.url("stage", "stage-1") + "?q=Clinique&priority=B&page=2"
        response = self.client.post(self.url("change_stage", self.hospital.pk),
                                    {"stage": "stage_2", "next": path})
        self.assertRedirects(response, path, fetch_redirect_response=False)

    def test_stage_post_rejects_external_next(self):
        response = self.client.post(self.url("change_stage", self.hospital.pk),
                                    {"stage": "stage_2", "next": "https://evil.example/"})
        self.assertRedirects(response, self.url("detail", self.hospital.pk), fetch_redirect_response=False)

    def test_bulk_move_uses_same_service_and_history(self):
        with patch("portal.hospital_engagement_views.change_hospital_engagement_stage",
                   wraps=change_hospital_engagement_stage) as service:
            response = self.client.post(self.url("bulk_move", "stage-1"),
                                        {"stage": "stage_3", "engagements": [self.engagement.pk, self.second.engagement.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.call_count, 2)
        self.assertEqual(HospitalFollowUp.objects.filter(activity_type="stage_change").count(), 2)
        self.assertEqual(HospitalEngagement.objects.filter(stage="stage_3").count(), 2)

    def test_bulk_invalid_selection_atomic_no_partial_changes(self):
        response = self.client.post(self.url("bulk_move", "stage-1"),
                                    {"stage": "stage_2", "engagements": [self.engagement.pk, 999999]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(HospitalFollowUp.objects.exists())

    def test_stage_history_failure_rolls_back_stage(self):
        with patch("hospital_engagements.services.HospitalFollowUp.objects.create", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                change_hospital_engagement_stage(self.engagement, "stage_2", self.user)
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.stage, "stage_1")

    def test_business_form_excludes_stage_and_hospital_data(self):
        response = self.client.post(self.url("business", self.hospital.pk), {
            "priority": "A", "owner": self.other.pk, "demand_summary": "DES 原文",
            "special_requirements": "Sterile", "next_action": "Call", "next_follow_up_date": "2026-11-01",
            "stage": "stage_3", "name": "Must not change",
        })
        self.assertEqual(response.status_code, 302)
        self.engagement.refresh_from_db()
        self.hospital.refresh_from_db()
        self.assertEqual((self.engagement.priority, self.engagement.owner, self.engagement.stage),
                         ("A", self.other, "stage_1"))
        self.assertEqual(self.hospital.name, "Clinique Étoile")

    def test_invalid_priority_and_inactive_owner_rejected(self):
        inactive = User.objects.create_user("inactive-owner", is_active=False)
        for data in [{"priority": "X"}, {"priority": "B", "owner": inactive.pk}]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url("business", self.hospital.pk), data).status_code, 400)

    def test_multiple_departments_and_same_name_other_hospital(self):
        HospitalDepartment.objects.create(hospital=self.hospital, name="Surgery")
        HospitalDepartment.objects.create(hospital=self.second, name="Cardiology")
        self.assertEqual(self.hospital.engagement_departments.count(), 2)

    def test_department_active_unique_case_insensitive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            HospitalDepartment.objects.create(hospital=self.hospital, name="CARDIOLOGY")
        HospitalDepartment.objects.create(hospital=self.hospital, name="CARDIOLOGY", is_active=False)

    def test_department_duplicate_form_error(self):
        form = DepartmentForm({"name": " cardiology ", "is_active": "on"}, hospital=self.hospital)
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_department_crud_deactivates_not_deletes(self):
        response = self.client.post(self.record_url("departments"), {"name": "Surgery", "is_active": "on"})
        self.assertEqual(response.status_code, 302)
        department = HospitalDepartment.objects.get(hospital=self.hospital, name="Surgery")
        self.client.post(self.record_url("departments", department, "edit"), {"name": "Surgery II", "is_active": "on"})
        self.client.post(self.record_url("departments", department, "remove"))
        department.refresh_from_db()
        self.assertEqual(department.name, "Surgery II")
        self.assertFalse(department.is_active)

    def test_multiple_contacts(self):
        save_contact(HospitalContact(hospital=self.hospital, name="Marie", contact_type="purchasing"))
        self.assertEqual(self.hospital.engagement_contacts.count(), 2)

    def test_primary_contact_service_replaces_previous(self):
        self.contact.is_primary = True
        save_contact(self.contact)
        second = save_contact(HospitalContact(hospital=self.hospital, name="Marie", is_primary=True))
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_primary)
        self.assertTrue(second.is_primary)

    def test_primary_contact_database_constraint(self):
        self.contact.is_primary = True
        save_contact(self.contact)
        with self.assertRaises(IntegrityError), transaction.atomic():
            HospitalContact.objects.create(hospital=self.hospital, name="Duplicate", is_primary=True)

    def test_contact_form_can_replace_primary(self):
        self.contact.is_primary = True
        save_contact(self.contact)
        response = self.client.post(self.record_url("contacts"), {
            "name": "New primary", "contact_type": "purchasing", "is_primary": "on", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_primary)

    def test_primary_action(self):
        response = self.client.post(self.url("primary", self.hospital.pk, self.contact.pk))
        self.assertEqual(response.status_code, 302)
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.is_primary)

    def test_contact_deactivation_preserves_history(self):
        entry = self.communication(contact=self.contact)
        self.client.post(self.record_url("contacts", self.contact, "remove"))
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_active)
        self.assertEqual(HospitalFollowUp.objects.get(pk=entry.pk).contact_id, self.contact.pk)

    def test_cross_hospital_contact_department_rejected(self):
        foreign = HospitalDepartment.objects.create(hospital=self.second, name="Foreign")
        form = ContactForm({"name": "Bad", "contact_type": "doctor", "department": foreign.pk},
                           hospital=self.hospital)
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            save_contact(HospitalContact(hospital=self.hospital, department=foreign, name="Bad"))

    def test_invalid_contact_type(self):
        response = self.client.post(self.record_url("contacts"), {"name": "Bad", "contact_type": "invalid"})
        self.assertEqual(response.status_code, 400)

    def test_cross_hospital_edit_and_remove_404(self):
        for action in ("edit", "remove"):
            url = self.url("record_" + action, self.second.pk, "contacts", self.contact.pk)
            self.assertEqual(self.client.post(url, {}).status_code, 404)
        self.assertEqual(self.client.post(self.url("primary", self.second.pk, self.contact.pk)).status_code, 404)

    def test_interest_product_only(self):
        form = InterestForm({"product": self.product.pk}, engagement=self.engagement)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

    def test_interest_text_only(self):
        form = InterestForm({"product_text": "DES 2.5–3.5mm"}, engagement=self.engagement)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

    def test_interest_product_and_text(self):
        form = InterestForm({"product": self.product.pk, "product_text": "DES"}, engagement=self.engagement)
        self.assertTrue(form.is_valid(), form.errors)

    def test_interest_empty_invalid(self):
        form = InterestForm({"product_text": "  "}, engagement=self.engagement)
        self.assertFalse(form.is_valid())

    def test_interest_cross_hospital_department_rejected(self):
        department = HospitalDepartment.objects.create(hospital=self.second, name="Foreign")
        form = InterestForm({"product_text": "DES", "department": department.pk}, engagement=self.engagement)
        self.assertFalse(form.is_valid())

    def test_interest_crud(self):
        self.assertEqual(self.client.post(self.record_url("interests"), {"product_text": "DES"}).status_code, 302)
        interest = self.engagement.product_interests.get()
        self.client.post(self.record_url("interests", interest, "edit"), {"product_text": "Updated DES"})
        interest.refresh_from_db()
        self.assertEqual(interest.product_text, "Updated DES")
        self.client.post(self.record_url("interests", interest, "remove"))
        self.assertFalse(HospitalProductInterest.objects.filter(pk=interest.pk).exists())

    def test_product_delete_keeps_interest(self):
        interest = HospitalProductInterest.objects.create(engagement=self.engagement, product=self.product)
        self.product.delete()
        interest.refresh_from_db()
        self.assertIsNone(interest.product_id)

    def test_communication_author_and_type(self):
        response = self.client.post(self.url("follow_up", self.hospital.pk),
                                    self.followup_data(created_by=self.other.pk, activity_type="stage_change"))
        self.assertEqual(response.status_code, 302)
        entry = self.engagement.follow_ups.get()
        self.assertEqual((entry.created_by, entry.activity_type), (self.user, "communication"))

    def test_communication_syncs_action(self):
        self.communication(next_action="Send catalogue")
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.next_action, "Send catalogue")

    def test_communication_syncs_date(self):
        self.communication(next_follow_up_date=date(2026, 11, 2))
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.next_follow_up_date, date(2026, 11, 2))

    def test_empty_next_fields_do_not_clear_existing(self):
        self.engagement.next_action = "Keep"
        self.engagement.next_follow_up_date = date(2026, 12, 1)
        self.engagement.save()
        self.communication()
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.next_action, "Keep")
        self.assertEqual(self.engagement.next_follow_up_date, date(2026, 12, 1))

    def test_communication_validation_and_cross_hospital_scoping(self):
        foreign_contact = HospitalContact.objects.create(hospital=self.second, name="Foreign")
        foreign_department = HospitalDepartment.objects.create(hospital=self.second, name="Foreign")
        for data in [self.followup_data(channel="invalid"), self.followup_data(summary=""),
                     self.followup_data(contact=foreign_contact.pk), self.followup_data(department=foreign_department.pk)]:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url("follow_up", self.hospital.pk), data).status_code, 400)
        self.assertFalse(self.engagement.follow_ups.exists())

    def test_timeline_order_actual_time_then_id(self):
        now = timezone.now()
        first = self.communication(occurred_at=now)
        later_id = self.communication(occurred_at=now)
        old = self.communication(occurred_at=now - timedelta(days=3))
        self.assertEqual(list(self.engagement.follow_ups.values_list("pk", flat=True)),
                         [later_id.pk, first.pk, old.pk])

    def test_latest_communication_ignores_stage_and_note(self):
        entry = self.communication(occurred_at=timezone.now() - timedelta(days=2))
        change_hospital_engagement_stage(self.engagement, "stage_2", self.user)
        HospitalFollowUp.objects.create(engagement=self.engagement, activity_type="note", summary="Internal")
        row = stage_queryset("stage_2", {}).get(pk=self.engagement.pk)
        self.assertEqual(row.latest_communication_at, entry.occurred_at)
        self.assertEqual(row.latest_communication_summary, "Business 原文")

    def test_communication_failure_rolls_back(self):
        with patch.object(HospitalEngagement, "save", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                self.communication(next_action="Should rollback")
        self.assertFalse(self.engagement.follow_ups.exists())

    def test_search_hospital(self):
        self.assertEqual(self.stage(q="Éto").context["page_obj"].paginator.count, 1)

    def test_search_contact(self):
        self.assertEqual(self.stage(q="Martin").context["page_obj"].paginator.count, 1)

    def test_search_product(self):
        HospitalProductInterest.objects.create(engagement=self.engagement, product=self.product)
        self.assertEqual(self.stage(q="2.501").context["page_obj"].paginator.count, 1)

    def test_search_free_text(self):
        HospitalProductInterest.objects.create(engagement=self.engagement, product_text="Full DES range")
        self.assertEqual(self.stage(q="DES").context["page_obj"].paginator.count, 1)

    def test_owner_filter(self):
        self.engagement.owner = self.user
        self.engagement.save()
        self.assertEqual(self.stage(owner=self.user.pk).context["page_obj"].paginator.count, 1)

    def test_priority_filter(self):
        self.engagement.priority = "A"
        self.engagement.save()
        self.assertEqual(self.stage(priority="A").context["page_obj"].paginator.count, 1)

    def test_invalid_filters_rejected(self):
        self.assertEqual(self.stage(priority="X").status_code, 400)
        self.assertEqual(self.stage(owner=999999).status_code, 400)

    def test_search_distinct(self):
        for i in range(3):
            HospitalProductInterest.objects.create(engagement=self.engagement, product_text=f"DES {i}")
            HospitalContact.objects.create(hospital=self.hospital, name=f"Doctor {i}")
        page = self.stage(q="DES").context["page_obj"]
        self.assertEqual(page.paginator.count, 1)
        self.assertEqual(len(page.object_list), 1)

    def test_pagination_preserves_filters(self):
        for i in range(41):
            hospital = Hospital.objects.create(name=f"Search {i:02}")
            HospitalEngagement.objects.filter(hospital=hospital).update(owner=self.user)
        response = self.stage(q="Search", priority="B", owner=self.user.pk)
        self.assertEqual(len(response.context["page_obj"]), 40)
        self.assertContains(response, f'q=Search&amp;priority=B&amp;owner={self.user.pk}&amp;page=2')
        page2 = self.stage(q="Search", priority="B", owner=self.user.pk, page=2)
        self.assertEqual(len(page2.context["page_obj"]), 1)

    def test_queryset_no_n_plus_one(self):
        for i in range(8):
            hospital = Hospital.objects.create(name=f"Performance {i}")
            HospitalContact.objects.create(hospital=hospital, name="Contact", is_primary=True)
            HospitalProductInterest.objects.create(engagement=hospital.engagement, product=self.product)
        with self.assertNumQueries(4):
            rows = decorate_rows(list(stage_queryset("stage_1", {})))
            for row in rows:
                str(row.hospital.name)
                str(row.primary_contact)
                str(row.interest_labels)
                str(row.owner)

    def test_stage_shipping_address_preferred(self):
        response = self.stage(q="Clinique")
        self.assertContains(response, "Shipping 原文")
        self.assertNotContains(response, "Billing 原文")
        self.assertNotContains(response, "Public contact")

    def test_detail_hospital_readonly_fields_and_timeline_types(self):
        self.communication()
        change_hospital_engagement_stage(self.engagement, "stage_2", self.user)
        HospitalFollowUp.objects.create(engagement=self.engagement, activity_type="note", summary="Note 原文")
        response = self.client.get(self.url("detail", self.hospital.pk))
        for text in ["FAX-123", "Public contact", "Billing 原文", "Shipping 原文", "Note 原文",
                     "he-event-communication", "he-event-stage_change", "he-event-note"]:
            self.assertContains(response, text)
        self.assertContains(response, reverse("portal:library_hospital_detail", args=[self.hospital.pk]))

    def test_detail_and_all_edit_forms_render(self):
        interest = HospitalProductInterest.objects.create(engagement=self.engagement, product_text="DES")
        for kind, record in [("departments", self.department), ("contacts", self.contact), ("interests", interest)]:
            for action, obj in [("add", None), ("edit", record)]:
                with self.subTest(kind=kind, action=action):
                    self.assertEqual(self.client.get(self.record_url(kind, obj, action)).status_code, 200)

    def test_i18n_chinese(self):
        self.client.cookies["django_language"] = "zh-hans"
        self.assertContains(self.client.get(self.url("home")), "医院沟通进度")
        self.assertContains(self.stage(), "第一阶段")

    def test_i18n_english(self):
        self.client.cookies["django_language"] = "en"
        self.assertContains(self.client.get(self.url("home")), "Hospital Engagement")
        response = self.stage()
        self.assertContains(response, "Stage 1")
        self.assertContains(response, "Priority")
        self.assertContains(response, "Save")

    def test_i18n_french(self):
        self.client.cookies["django_language"] = "fr"
        self.assertContains(self.client.get(self.url("home")), "Suivi des hôpitaux")
        response = self.stage()
        self.assertContains(response, "Étape 1")
        self.assertContains(response, "Priorité")
        self.assertContains(response, "Enregistrer")

    def test_i18n_business_data_invariant(self):
        self.engagement.special_requirements = "特殊原始需求"
        self.engagement.save()
        self.communication(summary="原始沟通 résumé")
        for language in ("zh-hans", "en", "fr"):
            self.client.cookies["django_language"] = language
            response = self.client.get(self.url("detail", self.hospital.pk))
            for value in ("Clinique Étoile", "特殊原始需求", "原始沟通 résumé", "Dr Martin"):
                self.assertContains(response, value)
        self.engagement.refresh_from_db()
        self.assertEqual(self.engagement.special_requirements, "特殊原始需求")

    def test_language_switch_preserves_module_path_query(self):
        path = self.url("stage", "stage-1") + "?q=DES&priority=A&page=2"
        for language in ("zh-hans", "en", "fr"):
            response = self.client.post(reverse("set_language"), {"language": language, "next": path})
            self.assertRedirects(response, path, fetch_redirect_response=False)

    def test_hospital_library_representative_pages(self):
        for name, args in [("library_hospitals", []), ("library_hospital_detail", [self.hospital.pk])]:
            response = self.client.get(reverse("portal:" + name, args=args))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Clinique Étoile")


class InitializationMigrationTests(TransactionTestCase):
    def test_existing_hospitals_initialized_idempotently_without_order_inference(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("hospital_engagements", "0001_initial")])
        try:
            historical = executor.loader.project_state().apps
            HospitalModel = historical.get_model("hospitals", "Hospital")
            EngagementModel = historical.get_model("hospital_engagements", "HospitalEngagement")
            OrderModel = historical.get_model("orders", "Order")
            active = HospitalModel.objects.create(name="Existing ordered hospital", billing_address="Unchanged")
            inactive = HospitalModel.objects.create(name="Existing inactive hospital", is_active=False)
            historical_user = historical.get_model("auth", "User").objects.create(username="migration-owner")
            OrderModel.objects.create(bon_de_commande="BEFORE-ENGAGEMENT", hospital=active, created_by=historical_user)
            self.assertFalse(EngagementModel.objects.filter(hospital_id=active.pk).exists())
            executor = MigrationExecutor(connection)
            executor.migrate([("hospital_engagements", "0002_initialize_engagements")])
            self.assertEqual(EngagementModel.objects.filter(hospital_id__in=[active.pk, inactive.pk]).count(), 2)
            engagement = EngagementModel.objects.get(hospital_id=active.pk)
            self.assertEqual((engagement.stage, engagement.priority, engagement.owner_id), ("stage_1", "B", None))
            engagement.stage = "stage_3"
            engagement.priority = "A"
            engagement.save()
            migration = import_module("hospital_engagements.migrations.0002_initialize_engagements")
            with connection.schema_editor() as editor:
                migration.initialize_engagements(historical, editor)
            engagement.refresh_from_db()
            self.assertEqual((engagement.stage, engagement.priority), ("stage_3", "A"))
            active.refresh_from_db()
            self.assertEqual(active.billing_address, "Unchanged")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
