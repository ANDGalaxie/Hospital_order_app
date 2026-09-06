from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from factory_confirmations.models import (
    FactoryConfirmation,
)
from orders.models import Order


class PricingAuditCommandTests(TestCase):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_user(
                username="pricing-audit-test",
                password="test",
            )
        )
        self.order = Order.objects.create(
            bon_de_commande="AUDIT-ORDER-001",
            hospital_order_pdf=(
                "hospital_orders/audit.pdf"
            ),
            extracted_order_data={
                "header": {
                    "order_date": "2026-03-01",
                }
            },
            created_by=self.user,
        )
        self.confirmation = (
            FactoryConfirmation.objects.create(
                order=self.order,
                confirmation_pdf=(
                    "factory_confirmations/audit.pdf"
                ),
                extracted_confirmation_data={
                    "factory_document": {
                        "shipping_date_only_iso": (
                            "2026-07-27"
                        ),
                    },
                    "serial_items": [],
                    "django": {
                        "kept": True,
                    },
                },
                created_by=self.user,
            )
        )

    def test_default_is_dry_run_and_apply_only_backfills_dates(
        self,
    ):
        stdout = StringIO()
        call_command(
            "audit_pricing_reference_dates",
            stdout=stdout,
        )

        self.order.refresh_from_db()
        self.confirmation.refresh_from_db()
        self.assertIsNone(self.order.order_date)
        self.assertIsNone(
            self.confirmation.shipping_date
        )
        self.assertIn(
            "DRY_RUN SUMMARY",
            stdout.getvalue(),
        )

        stdout = StringIO()
        call_command(
            "audit_pricing_reference_dates",
            "--apply-date-backfill",
            stdout=stdout,
        )

        self.order.refresh_from_db()
        self.confirmation.refresh_from_db()
        self.assertEqual(
            self.order.order_date.isoformat(),
            "2026-03-01",
        )
        self.assertEqual(
            self.confirmation
            .shipping_date
            .isoformat(),
            "2026-07-27",
        )
        self.assertEqual(
            self.confirmation
            .extracted_confirmation_data[
                "django"
            ],
            {"kept": True},
        )
        self.assertIn(
            "APPLY_DATE_BACKFILL SUMMARY",
            stdout.getvalue(),
        )

    def test_incomplete_factory_data_requires_manual_review(
        self,
    ):
        self.confirmation.extracted_confirmation_data = {
            "factory_document": {
                "shipping_date_only_iso": (
                    "2026-07-27"
                ),
            }
        }
        self.confirmation.save(
            update_fields=[
                "extracted_confirmation_data",
                "updated_at",
            ]
        )

        stdout = StringIO()
        call_command(
            "audit_pricing_reference_dates",
            "--apply-date-backfill",
            stdout=stdout,
        )
        self.confirmation.refresh_from_db()

        self.assertIsNone(
            self.confirmation.shipping_date
        )
        self.assertIn(
            "CONFIRMATION_MANUAL_REVIEW",
            stdout.getvalue(),
        )
