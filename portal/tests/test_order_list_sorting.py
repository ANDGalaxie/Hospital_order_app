from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from orders.models import Order
from portal.services.order_portal_service import build_order_list_context


class HospitalOrderListSortingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="order-list-sorting",
            password="test",
            is_staff=True,
        )
        self.request_factory = RequestFactory()

    def create_order(
        self,
        bon_de_commande,
        *,
        hospital_name="Sorting Hospital",
        extraction_status=Order.ExtractionStatus.NOT_STARTED,
    ):
        return Order.objects.create(
            bon_de_commande=bon_de_commande,
            hospital_name=hospital_name,
            hospital_order_pdf="hospital_orders/sorting-test.pdf",
            extraction_status=extraction_status,
            created_by=self.user,
        )

    def listed_order_numbers(self, **params):
        request = self.request_factory.get("/portal/orders/", params)
        request.user = self.user
        request.session = {}
        context = build_order_list_context(request)
        return [row["order_number"] for row in context["rows"]]

    def test_numeric_orders_are_sorted_descending(self):
        for bon in ["145342", "155141", "150222", "154753"]:
            self.create_order(bon)

        self.assertEqual(
            self.listed_order_numbers(),
            ["155141", "154753", "150222", "145342"],
        )

    def test_different_width_numbers_use_numeric_not_string_order(self):
        for bon in ["99999", "100000", "155672"]:
            self.create_order(bon)

        self.assertEqual(
            self.listed_order_numbers(),
            ["155672", "100000", "99999"],
        )

    def test_non_numeric_orders_follow_numeric_orders(self):
        for bon in ["155672", "UPLOAD-123", "UNKNOWN", "150222"]:
            self.create_order(bon)

        listed = self.listed_order_numbers()
        self.assertEqual(listed[:2], ["155672", "150222"])
        self.assertEqual(set(listed[2:]), {"UPLOAD-123", "UNKNOWN"})

    def test_non_numeric_orders_use_descending_id_as_stable_fallback(self):
        older = self.create_order("UPLOAD-123")
        newer = self.create_order("UNKNOWN")

        self.assertGreater(newer.id, older.id)
        self.assertEqual(
            self.listed_order_numbers(),
            ["UNKNOWN", "UPLOAD-123"],
        )

    def test_search_results_remain_numerically_sorted(self):
        self.create_order("99999", hospital_name="Matched Hospital")
        self.create_order("155672", hospital_name="Matched Hospital")
        self.create_order("200000", hospital_name="Different Hospital")

        self.assertEqual(
            self.listed_order_numbers(q="Matched"),
            ["155672", "99999"],
        )

    def test_status_filtered_results_remain_numerically_sorted(self):
        self.create_order("99999")
        self.create_order("155672")
        self.create_order(
            "200000",
            extraction_status=Order.ExtractionStatus.SUCCESS,
        )

        self.assertEqual(
            self.listed_order_numbers(status="pending_extraction"),
            ["155672", "99999"],
        )

    def test_recent_update_does_not_override_numeric_order(self):
        lower_bon = self.create_order("145342")
        higher_bon = self.create_order("155672")
        now = timezone.now()
        Order.objects.filter(pk=lower_bon.pk).update(updated_at=now)
        Order.objects.filter(pk=higher_bon.pk).update(
            updated_at=now - timedelta(days=1),
        )

        self.assertEqual(
            self.listed_order_numbers(),
            ["155672", "145342"],
        )
