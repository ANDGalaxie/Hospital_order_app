from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from orders.models import Order
from orders.services.hospital_order_extraction_service import (
    update_order_basic_fields_from_extracted_data,
)
from orders.services.order_date_service import (
    resolve_extracted_order_date,
)


class OrderDateServiceTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="order-date-test",
                password="test",
            )
        )
        self.order = Order.objects.create(
            bon_de_commande="ORDER-DATE-001",
            hospital_name="",
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            created_by=self.user,
        )

    def base_data(self):
        return {
            "header": {},
            "summary": {},
            "hospital": {},
            "factory": {},
            "addresses": {},
            "warnings": [],
        }

    def test_header_precedes_summary(self):
        parsed, source = (
            resolve_extracted_order_date(
                {
                    "header": {
                        "order_date": "2026-03-01",
                    },
                    "summary": {
                        "order_date": "2026-03-02",
                    },
                }
            )
        )
        self.assertEqual(
            parsed,
            date(2026, 3, 1),
        )
        self.assertEqual(
            source,
            "header.order_date",
        )

    def test_summary_is_safe_fallback(self):
        parsed, source = (
            resolve_extracted_order_date(
                {
                    "header": {},
                    "summary": {
                        "order_date": "01/03/2026",
                    },
                }
            )
        )
        self.assertEqual(
            parsed,
            date(2026, 3, 1),
        )
        self.assertEqual(
            source,
            "summary.order_date",
        )

    def test_extraction_persists_parsed_date_without_guessing(
        self,
    ):
        data = self.base_data()
        data["header"]["order_date"] = (
            "01/03/2026"
        )

        update_order_basic_fields_from_extracted_data(
            self.order,
            data,
        )

        self.assertEqual(
            self.order.order_date,
            date(2026, 3, 1),
        )
        self.assertEqual(
            data["django"]["order_date_source"],
            "header.order_date",
        )

        invalid = self.base_data()
        invalid["header"]["order_date"] = (
            "not-a-date"
        )
        update_order_basic_fields_from_extracted_data(
            self.order,
            invalid,
        )

        self.assertIsNone(
            self.order.order_date
        )
        self.assertEqual(
            self.order.document_validation_status,
            Order.DocumentValidationStatus.NEEDS_REVIEW,
        )
        self.assertIn(
            "需要人工检查",
            " ".join(invalid["warnings"]),
        )
