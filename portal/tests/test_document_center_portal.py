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
            hospital_name=(
                "DOCUMENT TEST HOSPITAL"
            ),
            hospital_order_pdf=(
                "hospital_orders/test.pdf"
            ),
            factory=self.factory,
            created_by=self.user,
        )

        self.invoice = (
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

        self.po = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_PO
                ),
                document_number=(
                    "PO-DOC-TEST-001"
                ),
                generated_by=self.user,
                source_data={
                    "items": [
                        {
                            "product_code": "TEST-01",
                            "quantity_raw": 2,
                            "unit_price_raw": 120,
                            "amount_raw": 240,
                        }
                    ],
                    "totals": {
                        "total_raw": 240,
                    },
                },
            )
        )

        self.request_document = (
            GeneratedDocument.objects.create(
                order=self.order,
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_ORDER_REQUEST
                ),
                document_number=(
                    "REQUEST-DOC-TEST-001"
                ),
                generated_by=self.user,
                source_data={},
            )
        )

    def test_document_home_has_three_categories(
        self,
    ):
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
            "Hospital Invoice",
        )

        self.assertContains(
            response,
            "Factory Purchase Order",
        )

        self.assertContains(
            response,
            "Factory Order Request",
        )

    def test_invoice_page_only_shows_invoices(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:document_invoices"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.invoice.document_number,
        )

        self.assertNotContains(
            response,
            self.po.document_number,
        )

    def test_po_page_only_shows_pos(self):
        response = self.client.get(
            reverse(
                "portal:document_factory_pos"
            )
        )

        self.assertContains(
            response,
            self.po.document_number,
        )

        self.assertNotContains(
            response,
            self.invoice.document_number,
        )

    def test_request_page_only_shows_requests(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:document_factory_requests"
            )
        )

        self.assertContains(
            response,
            self.request_document.document_number,
        )

        self.assertNotContains(
            response,
            self.invoice.document_number,
        )

    def test_all_documents_page(self):
        response = self.client.get(
            reverse(
                "portal:document_list"
            )
        )

        self.assertContains(
            response,
            self.invoice.document_number,
        )

        self.assertContains(
            response,
            self.po.document_number,
        )

        self.assertContains(
            response,
            self.request_document.document_number,
        )

    def test_request_page_hides_amount_column(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:document_factory_requests"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertFalse(
            response.context[
                "show_amount_column"
            ]
        )

        self.assertNotContains(
            response,
            ">金额<",
        )

        self.assertContains(
            response,
            self.request_document.document_number,
        )

    def test_invoice_page_keeps_amount_column(
        self,
    ):
        response = self.client.get(
            reverse(
                "portal:document_invoices"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.context[
                "show_amount_column"
            ]
        )

        self.assertContains(
            response,
            ">金额<",
        )

        self.assertContains(
            response,
            "500.00",
        )

    def test_document_detail_page(self):
        response = self.client.get(
            reverse(
                "portal:document_detail",
                args=[self.invoice.id],
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertContains(
            response,
            self.invoice.document_number,
        )

        self.assertContains(
            response,
            "TEST-01",
        )


    def test_pdf_url_contains_file_version_marker(self):
        from datetime import datetime, timezone
        from unittest.mock import Mock

        from portal.services.document_center_portal_service import (
            build_file_info,
        )

        field = Mock()
        field.name = "generated/test.pdf"
        field.url = "/media/generated/test.pdf"
        field.storage.exists.return_value = True
        field.storage.get_modified_time.return_value = datetime(
            2026,
            7,
            30,
            12,
            0,
            tzinfo=timezone.utc,
        )
        field.storage.size.return_value = 123

        info = build_file_info(field)

        self.assertIn("?v=", info["url"])
        self.assertIn("1785412800000000", info["url"])
