from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.db import connection
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from hospital_engagements.boss_access import BOSS_USERNAME, is_boss_user
from hospital_engagements.models import (
    ACCESS_PERMISSION, HospitalContact, HospitalDepartment, HospitalEngagement, HospitalFollowUp,
)
from hospitals.models import Hospital
from portal.services.home_portal_service import build_home_context
from portal.services.team_activity_service import (
    build_team_activity_context, period_bounds, salesperson_candidates,
)
from portal.team_activity_views import team_activity

User = get_user_model()


class TeamActivityAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_user("Acoeur", is_staff=True)
        cls.similar = User.objects.create_user("acoeurs", is_staff=True)
        cls.staff = User.objects.create_user("Claire", is_staff=True)
        cls.admin = User.objects.create_user("another-admin", is_staff=True, is_superuser=True)
        cls.url = reverse("portal:team_activity")

    def home_modules(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("portal:home")).context["modules"]

    def test_exact_boss_constant_and_active_authenticated_semantics(self):
        self.assertEqual(BOSS_USERNAME, "Acoeur")
        self.assertTrue(is_boss_user(self.boss))
        self.assertFalse(is_boss_user(AnonymousUser()))
        for name in ("acoeur", "ACOEUR", "acoeurs", "Acoeur2", " Acoeur"):
            with self.subTest(name=name):
                self.assertFalse(is_boss_user(User(username=name, is_active=True, is_superuser=True)))
        self.boss.is_active = False
        self.assertFalse(is_boss_user(self.boss))

    def test_boss_home_card_without_engagement_permission(self):
        self.assertFalse(self.boss.has_perm(ACCESS_PERMISSION))
        modules = self.home_modules(self.boss)
        self.assertEqual(len([m for m in modules if m["url"] == self.url]), 1)
        self.assertNotIn(reverse("portal:engagement_home"), [m["url"] for m in modules])

    def test_acoeurs_home_card_hidden(self):
        self.assertNotIn(self.url, [m["url"] for m in self.home_modules(self.similar)])

    def test_other_staff_home_card_hidden_even_with_engagement_permission(self):
        self.staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="hospital_engagements", codename="access_hospital_engagement",
        ))
        modules = self.home_modules(self.staff)
        self.assertNotIn(self.url, [m["url"] for m in modules])
        self.assertIn(reverse("portal:engagement_home"), [m["url"] for m in modules])

    def test_other_superuser_home_card_hidden(self):
        self.assertNotIn(self.url, [m["url"] for m in self.home_modules(self.admin)])

    def test_boss_get_200_without_permission_or_staff_requirement(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.boss.is_staff = False
        self.boss.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.boss.user_permissions.count(), 0)
        self.assertEqual(self.boss.groups.count(), 0)

    def test_acoeurs_direct_url_403(self):
        self.client.force_login(self.similar)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_direct_url_403_with_permission(self):
        self.staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="hospital_engagements", codename="access_hospital_engagement",
        ))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_other_superuser_direct_url_403(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_and_inactive_are_denied(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.boss.is_active = False
        request = RequestFactory().get(self.url)
        request.user = self.boss
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            team_activity(request)
        self.assertNotIn(self.url, [m["url"] for m in build_home_context(request)["modules"]])

    def test_read_only_endpoint(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_no_permission_added_and_no_entry_in_sales_pages(self):
        self.assertFalse(Permission.objects.filter(
            content_type__app_label="hospital_engagements", codename="view_team_activity",
        ).exists())
        self.client.force_login(self.admin)
        hospital = Hospital.objects.create(name="Access smoke hospital")
        response = self.client.get(reverse("portal:engagement_home"))
        self.assertEqual(len(response.context["cards"]), 3)
        for url in (
            reverse("portal:engagement_home"),
            reverse("portal:engagement_stage", args=["stage-1"]),
            reverse("portal:engagement_detail", args=[hospital.pk]),
        ):
            self.assertNotContains(self.client.get(url), self.url)


class TeamActivityDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.claire = User.objects.create_user("Claire", is_staff=True)
        cls.alice = User.objects.create_user("Alice", is_staff=True)
        cls.boss = User.objects.create_user("Acoeur", is_staff=True)
        cls.hospital = Hospital.objects.create(name="Hospital A")
        cls.second = Hospital.objects.create(name="Hospital B")
        cls.engagement = cls.hospital.engagement
        cls.engagement.owner = cls.alice
        cls.engagement.save(update_fields=["owner"])

    def setUp(self):
        self.tz = timezone.override(ZoneInfo("America/New_York"))
        self.tz.__enter__()
        self.addCleanup(self.tz.__exit__, None, None, None)
        self.now = self.at(2026, 10, 6, 12)
        self.client.force_login(self.boss)

    def at(self, year, month, day, hour=0, minute=0):
        return timezone.make_aware(datetime(year, month, day, hour, minute))

    def followup(self, **changes):
        values = dict(engagement=self.engagement, created_by=self.claire, occurred_at=self.now,
                      activity_type="communication", channel="phone", summary="Discussion")
        values.update(changes)
        return HospitalFollowUp.objects.create(**values)

    def advance(self, source="stage_1", target="stage_2", **changes):
        return self.followup(activity_type="stage_change", stage_from=source, stage_to=target, **changes)

    def context(self, **params):
        query = QueryDict(mutable=True)
        query.update(params)
        return build_team_activity_context(query, now=self.now)

    def test_distinct_hospitals_and_communication_count_exclude_other_types(self):
        self.followup()
        self.followup()
        self.followup(engagement=self.second.engagement, channel="email")
        self.followup(activity_type="note")
        self.advance()
        metrics = self.context()["metrics"]
        self.assertEqual(metrics["contacted_hospitals"], 2)
        self.assertEqual(metrics["communications"], 3)

    def test_occurred_at_not_created_at(self):
        entry = self.followup(occurred_at=self.now - timedelta(days=1))
        HospitalFollowUp.objects.filter(pk=entry.pk).update(created_at=self.now)
        self.assertEqual(self.context()["metrics"]["communications"], 0)
        self.assertEqual(self.context(period="custom", start="2026-10-05", end="2026-10-05")["metrics"]["communications"], 1)

    def test_today_uses_local_midnight_half_open_interval(self):
        start = self.at(2026, 10, 6)
        for instant in (start - timedelta(microseconds=1), start, self.at(2026, 10, 7) - timedelta(microseconds=1), self.at(2026, 10, 7)):
            self.followup(occurred_at=instant)
        self.assertEqual(self.context()["metrics"]["communications"], 2)

    def test_week_monday_boundary_and_no_future(self):
        monday = self.at(2026, 10, 5)
        for instant in (monday - timedelta(microseconds=1), monday, self.now - timedelta(seconds=1), self.now + timedelta(seconds=1), self.at(2026, 10, 11)):
            self.followup(occurred_at=instant)
        context = self.context(period="week")
        self.assertEqual(context["period_start"], monday)
        self.assertEqual(context["period_end"], self.now)
        self.assertEqual(context["metrics"]["communications"], 2)

    def test_custom_inclusive_end(self):
        self.followup(occurred_at=self.at(2026, 10, 5))
        self.followup(occurred_at=self.at(2026, 10, 6, 23, 59))
        self.followup(occurred_at=self.at(2026, 10, 7))
        self.assertEqual(self.context(period="custom", start="2026-10-05", end="2026-10-06")["metrics"]["communications"], 2)

    def test_dst_calendar_day_is_not_fixed_24_hours(self):
        for day, hours in ((date(2026, 3, 8), 23), (date(2026, 11, 1), 25)):
            with self.subTest(day=day):
                start, end = period_bounds({"period": "custom", "start": day, "end": day}, self.now)
                self.assertEqual((end.timestamp() - start.timestamp()) / 3600, hours)

    def test_custom_invalid_dates_and_range(self):
        for params in (
            {"start": "2026-10-07", "end": "2026-10-06"},
            {"start": "bad", "end": "2026-10-06"},
            {"start": "2026-10-05"}, {"start": "9999-12-31", "end": "9999-12-31"},
        ):
            with self.subTest(params=params):
                self.assertFalse(self.context(period="custom", **params)["valid_filters"])

    def test_invalid_filters_do_not_silently_show_unfiltered_facts(self):
        for params in ({"period": "bad"}, {"salesperson": "999999"}, {"salesperson": "abc"}):
            context = self.context(**params)
            self.assertFalse(context["valid_filters"])
            self.assertNotIn("metrics", context)

    def test_all_forward_pairs_count(self):
        for source, target in (("stage_1", "stage_2"), ("stage_2", "stage_3"), ("stage_1", "stage_3")):
            hospital = Hospital.objects.create(name=source + target)
            self.advance(source, target, engagement=hospital.engagement)
        context = self.context()
        self.assertEqual(context["metrics"]["advanced_hospitals"], 3)
        self.assertEqual(context["metrics"]["entered_stage_2"], 1)
        self.assertEqual(context["metrics"]["entered_stage_3"], 2)
        self.assertEqual(context["advancements"]["page"].paginator.count, 3)

    def test_backward_same_invalid_and_non_stage_activity_do_not_advance(self):
        for source, target in (("stage_2", "stage_1"), ("stage_3", "stage_2"), ("stage_1", "stage_1"), ("", "stage_3"), ("stage_0", "stage_3"), ("stage_1", "invalid")):
            self.advance(source, target)
        self.followup(stage_from="stage_1", stage_to="stage_3")
        self.assertEqual(self.context()["metrics"]["advanced_hospitals"], 0)
        self.assertEqual(self.context()["advancements"]["page"].paginator.count, 0)
        self.assertEqual(HospitalFollowUp.objects.filter(activity_type="stage_change").count(), 6)

    def test_two_advancements_one_hospital_kpi_and_two_details(self):
        self.advance()
        self.advance("stage_2", "stage_3")
        context = self.context()
        self.assertEqual(context["metrics"]["advanced_hospitals"], 1)
        self.assertEqual(context["metrics"]["entered_stage_2"], 1)
        self.assertEqual(context["metrics"]["entered_stage_3"], 1)
        self.assertEqual(context["advancements"]["page"].paginator.count, 2)

    def test_activity_attribution_uses_author_not_current_owner(self):
        self.followup()
        self.advance()
        claire = self.context(salesperson=str(self.claire.pk))
        alice = self.context(salesperson=str(self.alice.pk))
        self.assertEqual(claire["metrics"]["communications"], 1)
        self.assertEqual(claire["metrics"]["advanced_hospitals"], 1)
        self.assertEqual(alice["metrics"]["communications"], 0)
        self.assertEqual(alice["metrics"]["advanced_hospitals"], 0)
        self.assertFalse(claire["show_summary"])

    def test_overdue_attribution_uses_owner_not_activity_author(self):
        self.followup()
        self.engagement.next_follow_up_date = date(2026, 10, 3)
        self.engagement.save(update_fields=["next_follow_up_date"])
        claire = self.context(salesperson=str(self.claire.pk))
        alice = self.context(salesperson=str(self.alice.pk))
        self.assertEqual(claire["overdue"]["page"].paginator.count, 0)
        self.assertEqual(alice["overdue"]["page"].paginator.count, 1)
        self.assertEqual(list(alice["overdue"]["page"])[0].overdue_days, 3)

    def test_overdue_excludes_today_future_null_and_inactive(self):
        for offset, active in ((-1, True), (0, True), (1, True), (-2, False), (None, True)):
            hospital = Hospital.objects.create(name=f"Due {offset} {active}", is_active=active)
            HospitalEngagement.objects.filter(hospital=hospital).update(
                next_follow_up_date=date(2026, 10, 6) + timedelta(days=offset) if offset is not None else None,
            )
        self.assertEqual(self.context()["overdue"]["page"].paginator.count, 1)

    def test_overdue_snapshot_independent_of_period(self):
        self.engagement.next_follow_up_date = date(2026, 10, 3)
        self.engagement.save(update_fields=["next_follow_up_date"])
        for params in ({}, {"period": "week"}, {"period": "custom", "start": "2020-01-01", "end": "2020-01-01"}):
            self.assertEqual(self.context(**params)["overdue"]["page"].paginator.count, 1)

    def test_latest_communication_excludes_newer_stage_change_and_note(self):
        self.engagement.next_follow_up_date = date(2026, 10, 3)
        self.engagement.save(update_fields=["next_follow_up_date"])
        self.followup(occurred_at=self.now - timedelta(days=20), summary="Earlier")
        latest = self.followup(occurred_at=self.now - timedelta(days=20), summary="Latest communication")
        self.advance()
        self.followup(activity_type="note")
        item = list(self.context()["overdue"]["page"])[0]
        self.assertEqual(item.latest_communication_at, latest.occurred_at)
        self.assertEqual(item.latest_communication_summary, "Latest communication")

    def test_summary_groups_by_author_and_global_distinct_is_not_sum(self):
        self.followup()
        self.followup(created_by=self.alice)
        self.advance(created_by=self.alice)
        context = self.context()
        rows = {row["created_by_id"]: row for row in context["summary"]}
        self.assertEqual(list(rows), [self.alice.pk, self.claire.pk])
        self.assertEqual(rows[self.claire.pk]["communications"], 1)
        self.assertEqual(rows[self.alice.pk]["advanced_hospitals"], 1)
        self.assertEqual(context["metrics"]["contacted_hospitals"], 1)

    def test_active_candidates_only_owners_or_authors(self):
        inactive = User.objects.create_user("Inactive", is_active=False)
        self.followup()
        self.followup(created_by=inactive)
        self.assertEqual(set(salesperson_candidates().values_list("pk", flat=True)), {self.alice.pk, self.claire.pk})

    def test_deleted_and_inactive_authors_preserve_historic_facts(self):
        inactive = User.objects.create_user("Inactive", is_active=False)
        self.followup(created_by=inactive)
        self.followup(created_by=None)
        context = self.context()
        self.assertEqual(context["metrics"]["communications"], 2)
        self.assertEqual(len(context["summary"]), 2)

    def test_details_order_by_occurrence_then_id_and_include_relations(self):
        department = HospitalDepartment.objects.create(hospital=self.hospital, name="Cardiology")
        contact = HospitalContact.objects.create(hospital=self.hospital, department=department, name="Doctor")
        first = self.followup(contact=contact, department=department)
        second = self.followup()
        old = self.followup(occurred_at=self.now - timedelta(hours=1))
        page = self.context()["communications"]["page"]
        self.assertEqual([entry.pk for entry in page], [second.pk, first.pk, old.pk])

    def test_independent_pagination_retains_filters(self):
        for _ in range(31):
            self.followup()
            self.advance()
        context = self.context(communications_page="2", period="today", salesperson=str(self.claire.pk))
        self.assertEqual(context["communications"]["page"].number, 2)
        self.assertEqual(len(context["communications"]["page"]), 1)
        self.assertEqual(context["advancements"]["page"].number, 1)
        self.assertIn("salesperson=" + str(self.claire.pk), context["communications"]["previous_url"])
        self.assertIn("#communications", context["communications"]["previous_url"])
        self.assertEqual(context["metrics"]["communications"], 31)

    def test_full_page_query_count_does_not_grow_per_actor_or_hospital(self):
        self.followup()
        self.advance()
        self.engagement.next_follow_up_date = date(2026, 10, 1)
        self.engagement.save(update_fields=["next_follow_up_date"])
        request = RequestFactory().get(reverse("portal:team_activity"))
        request.user = self.boss
        with patch("portal.services.team_activity_service.timezone.now", return_value=self.now):
            with CaptureQueriesContext(connection) as small:
                self.assertEqual(team_activity(request).status_code, 200)
            for i in range(5):
                user = User.objects.create_user(f"Sales-{i}")
                hospital = Hospital.objects.create(name=f"Query hospital {i}")
                engagement = hospital.engagement
                engagement.owner = user
                engagement.next_follow_up_date = date(2026, 10, 1)
                engagement.save()
                self.followup(engagement=engagement, created_by=user)
                self.advance(engagement=engagement, created_by=user)
            with CaptureQueriesContext(connection) as large:
                self.assertEqual(team_activity(request).status_code, 200)
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 11)

    def test_get_does_not_write_business_or_user_data(self):
        self.followup()
        before = list(HospitalEngagement.objects.values())
        users = list(User.objects.values())
        followups = list(HospitalFollowUp.objects.values())
        self.client.get(reverse("portal:team_activity"))
        self.assertEqual(before, list(HospitalEngagement.objects.values()))
        self.assertEqual(users, list(User.objects.values()))
        self.assertEqual(followups, list(HospitalFollowUp.objects.values()))

    def test_three_languages_with_business_text_preserved(self):
        self.followup(summary="Business 原文")
        self.advance()
        self.engagement.next_follow_up_date = timezone.localdate() - timedelta(days=1)
        self.engagement.save(update_fields=["next_follow_up_date"])
        for code, title in (("zh-hans", "团队工作概览"), ("en", "Team Activity"), ("fr", "Activité de l’équipe")):
            with self.subTest(code=code):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = code
                with patch("portal.services.team_activity_service.timezone.now", return_value=self.now):
                    response = self.client.get(reverse("portal:team_activity"))
                self.assertContains(response, title)
                self.assertContains(response, "Business 原文")
                if code != "zh-hans":
                    self.assertNotRegex(response.content.decode().replace("中文", "").replace("Business 原文", ""), r"[\u4e00-\u9fff]")
                self.assertContains(self.client.get(reverse("portal:home")), title)

    def test_invalid_date_error_is_translated_and_returns_400(self):
        for code in ("en", "fr"):
            with translation.override(code):
                message = translation.gettext("开始日期不能晚于结束日期。")
                self.assertNotRegex(message, r"[\u4e00-\u9fff]")
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = code
            response = self.client.get(reverse("portal:team_activity"), {"period": "custom", "start": "2026-10-07", "end": "2026-10-06"})
            self.assertContains(response, message, status_code=400)

