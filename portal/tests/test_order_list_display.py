from datetime import date, datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from portal.services.common import get_global_numeric_bon_ordinals
from portal.services.order_portal_service import build_order_list_context
from portal.services.settlement_portal_service import build_order_batch_amount_rows
from shipments.models import ShipmentBatch


class HospitalOrderListDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("order-display", is_staff=True)
        cls.orders = {}
        # Deliberately create in an order unrelated to numeric BON sorting.
        for bon in ("156264", "143211", "150222", "145342"):
            cls.orders[bon] = Order.objects.create(
                bon_de_commande=bon, hospital_name="Display Hospital",
                hospital_order_pdf="hospital_orders/display-test.pdf",
                order_date=date(2026, 9, 28) if bon == "150222" else None,
                created_by=cls.user,
            )

    def setUp(self):
        self.client.force_login(self.user)

    def context(self, **params):
        request = RequestFactory().get(reverse("portal:order_list"), params)
        request.user = self.user
        return build_order_list_context(request)

    def response(self, **params):
        return self.client.get(reverse("portal:order_list"), params)

    def test_date_reads_order_date_and_displays_date_without_time(self):
        row = self.context(q="150222")["rows"][0]
        self.assertEqual(row["order_date"], date(2026, 9, 28))
        response = self.response(q="150222")
        self.assertContains(response, '<span class="sub-text">2026-09-28</span>', html=True)
        self.assertNotContains(response, "2026-09-28 00:00")

    def test_missing_order_date_displays_dash_without_timestamp_fallback(self):
        event_time = timezone.make_aware(datetime(2040, 2, 3, 4, 5))
        Order.objects.filter(pk=self.orders["143211"].pk).update(created_at=event_time, updated_at=event_time)
        response = self.response(q="143211")
        self.assertContains(response, '<span class="sub-text">—</span>', html=True)
        self.assertNotContains(response, "2040-02-03")
        self.assertIsNone(response.context["rows"][0]["order_date"])

    def test_updated_at_is_not_used_as_order_date(self):
        event_time = timezone.make_aware(datetime(2040, 2, 3, 4, 5))
        Order.objects.filter(pk=self.orders["150222"].pk).update(created_at=event_time, updated_at=event_time)
        response = self.response(q="150222")
        self.assertContains(response, "2026-09-28")
        self.assertNotContains(response, "2040-02-03")
        self.assertNotContains(response, "更新时间")

    def test_global_numeric_ordinal_definition(self):
        self.assertEqual(get_global_numeric_bon_ordinals(), {
            self.orders["143211"].pk: 1, self.orders["145342"].pk: 2,
            self.orders["150222"].pk: 3, self.orders["156264"].pk: 4,
        })
        self.assertNotEqual(self.orders["156264"].pk, 4)

    def test_default_numeric_descending_order_is_unchanged(self):
        self.assertEqual(
            [(row["bon_ordinal"], row["order_number"]) for row in self.context()["rows"]],
            [(4, "156264"), (3, "150222"), (2, "145342"), (1, "143211")],
        )

    def test_search_retains_global_ordinal(self):
        response = self.response(q="150222")
        self.assertEqual([(row["bon_ordinal"], row["order_number"]) for row in response.context["rows"]], [(3, "150222")])
        self.assertContains(response, '<td class="bon-ordinal-cell"><span class="sub-text">3</span></td>', html=True)

    def test_page_parameter_preserves_current_non_paginated_behavior(self):
        # Current list has a 300-row cap and no pagination. Do not invent a new
        # page behavior in this display-only change.
        first = self.response()
        second = self.response(page=2)
        self.assertEqual(first.context["rows"], second.context["rows"])
        self.assertEqual(second.context["rows"][2]["bon_ordinal"], 2)

    def test_slicing_into_pages_never_rebuilds_ordinals(self):
        # Ordinals exist on the rows before any consumer slices/paginates them.
        page = Paginator(self.context()["rows"], 2).page(2)
        self.assertEqual([(row["bon_ordinal"], row["order_number"]) for row in page], [(2, "145342"), (1, "143211")])

    def test_non_numeric_bons_are_unranked_and_do_not_affect_official_bons(self):
        for bon in ("UPLOAD-123", "UNKNOWN", "TEMP-001", "12A34", "１２３"):
            Order.objects.create(bon_de_commande=bon, hospital_name="Temporary", created_by=self.user)
            row = self.context(q=bon)["rows"][0]
            self.assertIsNone(row["bon_ordinal"])
            self.assertContains(self.response(q=bon), '<td class="bon-ordinal-cell"><span class="sub-text">—</span></td>', html=True)
        self.assertEqual(len(get_global_numeric_bon_ordinals()), 4)
        self.assertEqual(get_global_numeric_bon_ordinals()[self.orders["156264"].pk], 4)

    def test_shared_helper_preserves_numeric_value_and_leading_zero_tie_break(self):
        lower = Order.objects.create(bon_de_commande="9", hospital_name="Numeric", created_by=self.user)
        padded = Order.objects.create(bon_de_commande="00010", hospital_name="Numeric", created_by=self.user)
        plain = Order.objects.create(bon_de_commande="10", hospital_name="Numeric", created_by=self.user)
        ordinals = get_global_numeric_bon_ordinals()
        self.assertEqual([ordinals[order.pk] for order in (lower, padded, plain)], [1, 2, 3])
        self.assertEqual(ordinals[self.orders["143211"].pk], 4)

    def test_list_and_settlement_have_identical_ordinals(self):
        for order in self.orders.values():
            ShipmentBatch.objects.create(
                order=order, source_type=ShipmentBatch.SourceType.MANUAL,
                batch_number=1, month_key="2026-10",
            )
        order_values = {row["id"]: row["bon_ordinal"] for row in self.context()["rows"]}
        settlement_values = {row["order_id"]: row["bon_ordinal"] for row in build_order_batch_amount_rows()}
        self.assertEqual(order_values, settlement_values)

    def test_orders_outside_300_row_display_cap_still_count(self):
        Order.objects.bulk_create([
            Order(bon_de_commande=str(200000 + number), hospital_name="Capped", created_by=self.user)
            for number in range(300)
        ])
        rows = self.context()["rows"]
        self.assertEqual(len(rows), 300)
        self.assertEqual(rows[0]["bon_ordinal"], 304)
        self.assertEqual(rows[-1]["bon_ordinal"], 5)
        self.assertEqual(self.context(q="150222")["rows"][0]["bon_ordinal"], 3)

    def test_helper_uses_one_query_for_all_orders(self):
        with self.assertNumQueries(1):
            first = get_global_numeric_bon_ordinals()
        Order.objects.bulk_create([
            Order(bon_de_commande=str(200000 + number), hospital_name="Query count", created_by=self.user)
            for number in range(10)
        ])
        with self.assertNumQueries(1):
            second = get_global_numeric_bon_ordinals()
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 14)

    def test_list_calls_shared_helper_once_not_per_row(self):
        with patch("portal.services.order_portal_service.get_global_numeric_bon_ordinals", wraps=get_global_numeric_bon_ordinals) as helper:
            rows = self.context()["rows"]
        self.assertEqual(len(rows), 4)
        helper.assert_called_once()

    def test_three_language_headers(self):
        for language, ordinal, date_label in (
            ("zh-hans", "BON 序号", "下单日期"),
            ("en", "BON No.", "Order Date"),
            ("fr", "N° BON", "Date de commande"),
        ):
            with self.subTest(language=language):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
                response = self.response(q="150222")
                self.assertContains(response, f'<th class="bon-ordinal-cell">{ordinal}</th>', html=True)
                self.assertContains(response, f"<th>{date_label}</th>", html=True)
                self.assertContains(response, "150222")
                self.assertContains(response, "Display Hospital")
                self.assertContains(response, "2026-09-28")

    def test_get_does_not_modify_order_business_data(self):
        before = list(Order.objects.order_by("pk").values())
        self.response()
        self.assertEqual(before, list(Order.objects.order_by("pk").values()))

    def test_empty_table_colspan_includes_new_column(self):
        response = self.response(q="no-match-at-all")
        self.assertContains(response, '<td colspan="9">')

