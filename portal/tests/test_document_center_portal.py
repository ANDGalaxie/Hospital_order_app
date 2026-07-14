from decimal import Decimal

from django.contrib.auth import (
    get_user_model,
)
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    GeneratedDocument,
)
from factories.models import Factory
from orders.models import Order


class DocumentCenterPortalTests(
    TestCase
):
    def setUp(self):
        self.user = (
            get_user_model()
            .objects.create_superuser(
                username="document-admin",
                email="document@example.com",
                password="test-password",
            )
        )

        self.client.force_login(self.user)

        self.factory = Factory.objects.create(
            name="DOCUMENT TEST FACTORY",
            short_name="DTF",
        )

        self.order = Order.objects.create(
            bon_de_commande="DOC-TEST-001",
            hospital_name="DOCUMENT TEST HOSPITAL",
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            created_by=self.user,
        )

        self.document = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
                document_number=(
                    "INVOICE-DOC-TEST-001"
                ),
                generated_by=self.user,
                source_data={
                    "invoice": {
                        "due_date": "31/12/2026",
                    },
                    "items": [
                        {
                            "product_code": "TEST-01",
                            "quantity_raw": 2,
                            "unit_price_raw": 250,
                            "amount_raw": 500,
                        }
                    ],
                    "totals": {
                        "total_raw": 500,
                        "total_units_raw": 2,
                    },
                },
            )
        )

    def test_document_list_page(self):
        response = self.client.get(
            reverse(
                "portal:document_center"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "INVOICE-DOC-TEST-001",
        )

        self.assertContains(
            response,
            "500.00",
        )

    def test_document_detail_page(self):
        response = self.client.get(
            reverse(
                "portal:document_detail",
                args=[self.document.id],
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            "INVOICE-DOC-TEST-001",
        )

        self.assertContains(
            response,
            "DOC-TEST-001",
        )

        self.assertContains(
            response,
            "TEST-01",
        )

    def test_type_filter(self):
        response = self.client.get(
            reverse(
                "portal:document_center"
            ),
            {
                "type": (
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                ),
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.document.document_number,
        )
