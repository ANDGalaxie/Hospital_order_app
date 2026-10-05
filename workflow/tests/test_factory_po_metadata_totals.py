from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from orders.models import Order
from shipments.models import ShipmentBatch
from workflow.services.workflow_document_generation_service import (
    build_batch_factory_po_data,
    render_po_html,
)


class FactoryPoMetadataAndTotalsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="po-metadata-totals",
            password="test",
        )
        self.order = Order.objects.create(
            bon_de_commande="155141",
            order_date=date(2026, 9, 2),
            hospital_name="PO Metadata Hospital",
            hospital_order_pdf="hospital_orders/po-metadata.pdf",
            shipping_address_data={"lines": ["1 Hospital Street"]},
            created_by=self.user,
        )
        self.batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=2,
            batch_date=date(2026, 9, 5),
            month_key="2026-09",
            total_requested_quantity=100,
            shipped_this_batch_quantity=3,
        )
        self.company_info = {
            "logo_path": "data/logo.png",
            "po_company": {
                "display_name": "DELA GLOBAL HK",
                "address": ["Hong Kong"],
            },
        }
        self.factory_info = {
            "factory_name": "Factory",
            "factory_address": ["1 Factory Street"],
            "buyer": "Dela Global Trade Consulting Limited",
        }
        self.numbers = {
            "po_number": "DELAHK0209S-B2",
            "batch_numbering_warnings": [],
        }

    def build_data(self, items):
        with patch(
            "workflow.services.workflow_document_generation_service."
            "build_batch_factory_po_items",
            return_value=(items, []),
        ):
            return build_batch_factory_po_data(
                self.batch,
                self.company_info,
                self.factory_info,
                self.numbers,
                factory_shipping_date=date(2026, 9, 5),
                factory_shipping_date_source="test",
            )

    def test_po_uses_original_hospital_bon_and_sums_current_batch_units(self):
        po_data = self.build_data(
            [
                {
                    "quantity_raw": 50,
                    "amount_raw": 5000,
                },
                {
                    "quantity_raw": 3,
                    "amount_raw": 300,
                },
            ]
        )

        self.assertEqual(po_data["po"]["bon_de_commande"], "155141")
        self.assertEqual(
            po_data["po"]["source"],
            "BON DE COMMANDE N° 155141",
        )
        self.assertNotIn("-B2", po_data["po"]["source"])
        self.assertEqual(po_data["totals"]["total_units_raw"], 53.0)
        self.assertEqual(po_data["totals"]["total_units"], "53")
        self.assertEqual(po_data["totals"]["total_raw"], 5300.0)

    def test_batch_two_total_units_does_not_use_order_total(self):
        po_data = self.build_data(
            [
                {
                    "quantity_raw": Decimal("3"),
                    "amount_raw": 360,
                }
            ]
        )

        self.assertEqual(po_data["totals"]["total_units_raw"], 3.0)
        self.assertEqual(po_data["totals"]["total_units"], "3")
        self.assertNotEqual(
            po_data["totals"]["total_units_raw"],
            self.batch.total_requested_quantity,
        )

    def test_rendered_po_has_four_metadata_fields_and_units_before_total(self):
        po_data = self.build_data(
            [
                {
                    "product_code": "PRODUCT-1",
                    "description": "Product",
                    "quantity_raw": 3,
                    "quantity": "3.00",
                    "unit_price": "120.00",
                    "discount": "0.00%",
                    "amount_raw": 360,
                    "amount": "360.00 €",
                    "discount_note": "",
                }
            ]
        )
        html = render_po_html(
            po_data=po_data,
            template_path=(
                Path(settings.BASE_DIR)
                / "templates"
                / "factory_purchase_order.html"
            ),
        )

        self.assertIn(">Buyer<", html)
        self.assertIn(">Source<", html)
        self.assertIn(">Order Date<", html)
        self.assertIn(">Expected Arrival<", html)
        self.assertIn("BON DE COMMANDE N° 155141", html)
        self.assertLess(html.index("Total Units"), html.index(">Total<"))
        self.assertIn(">3<", html)
