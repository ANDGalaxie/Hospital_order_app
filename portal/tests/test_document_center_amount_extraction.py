from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from documents.models import GeneratedDocument
from portal.services.document_center_portal_service import (
    extract_document_amount,
    get_document_payload,
)


class DocumentCenterAmountExtractionTests(
    SimpleTestCase
):
    def test_invoice_nested_amount(self):
        document = SimpleNamespace(
            document_type=(
                GeneratedDocument
                .DocumentType
                .HOSPITAL_INVOICE
            ),
            source_data={
                "workflow_item_id": 1,
                "invoice_data": {
                    "items": [],
                    "totals": {
                        "total_raw": 500,
                    },
                },
            },
        )

        self.assertEqual(
            extract_document_amount(
                document
            ),
            Decimal("500"),
        )

        self.assertIn(
            "totals",
            get_document_payload(
                document
            ),
        )

    def test_po_nested_amount(self):
        document = SimpleNamespace(
            document_type=(
                GeneratedDocument
                .DocumentType
                .FACTORY_PO
            ),
            source_data={
                "workflow_item_id": 1,
                "po_data": {
                    "items": [],
                    "totals": {
                        "total_raw": 204,
                    },
                },
            },
        )

        self.assertEqual(
            extract_document_amount(
                document
            ),
            Decimal("204"),
        )

    def test_old_top_level_amount(self):
        document = SimpleNamespace(
            document_type=(
                GeneratedDocument
                .DocumentType
                .HOSPITAL_INVOICE
            ),
            source_data={
                "totals": {
                    "total_raw": 300,
                },
            },
        )

        self.assertEqual(
            extract_document_amount(
                document
            ),
            Decimal("300"),
        )

    def test_request_without_amount(self):
        document = SimpleNamespace(
            document_type=(
                GeneratedDocument
                .DocumentType
                .FACTORY_ORDER_REQUEST
            ),
            source_data={
                "request_data": {
                    "items": [
                        {
                            "product_code": (
                                "TEST-01"
                            ),
                            "quantity": 2,
                        }
                    ],
                },
            },
        )

        self.assertIsNone(
            extract_document_amount(
                document
            )
        )
