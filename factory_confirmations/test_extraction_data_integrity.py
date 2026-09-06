import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase, override_settings

from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation, SerialItem
from factory_confirmations.services.factory_confirmation_extraction_service import (
    FactoryExtractionValidationError,
    attach_order_from_factory_confirmation_data,
    extract_factory_confirmation_for_confirmation,
    finalize_factory_confirmation_after_order_match,
    get_factory_confirmation_workspace,
    stamp_confirmation_debug_metadata,
    validate_factory_data_before_finalize,
)
from orders.models import Order, OrderItem
from products.models import Product
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem


class FactoryConfirmationDataIntegrityTests(TestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(
            MEDIA_ROOT=self.media_directory.name
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user = get_user_model().objects.create_user(
            username="factory-integrity-test",
            password="test-password",
        )
        self.factory = Factory.objects.create(
            name="Factory Integrity Test",
            short_name="FIT",
        )
        self.order = Order.objects.create(
            bon_de_commande="152870",
            hospital_name="Integrity Hospital",
            hospital_order_pdf="hospital_orders/152870.pdf",
            factory=self.factory,
            created_by=self.user,
        )

        self.product_codes = []
        quantities = [4] + [3] * 9
        for index, quantity in enumerate(quantities):
            product = Product.objects.create(
                code=f"INTEGRITY-{index:02d}",
                factory=self.factory,
            )
            self.product_codes.append(product.code)
            OrderItem.objects.create(
                order=self.order,
                product=product,
                product_code=product.code,
                requested_quantity=quantity,
                backordered_quantity=quantity,
            )

        self.confirmation = FactoryConfirmation.objects.create(
            order=self.order,
            factory=self.factory,
            confirmation_pdf=SimpleUploadedFile(
                "factory-confirmation-79.pdf",
                b"%PDF-1.4 regression fixture",
                content_type="application/pdf",
            ),
            created_by=self.user,
        )

    def full_factory_data(self):
        serial_items = []
        summary_by_product = []
        serial_index = 1

        for product_index, product_code in enumerate(self.product_codes):
            quantity = 4 if product_index == 0 else 3
            product_serials = []

            for _offset in range(quantity):
                serial_number = f"FC79-SERIAL-{serial_index:03d}"
                product_serials.append(serial_number)
                serial_items.append(
                    {
                        "product_code": product_code,
                        "serial_number": serial_number,
                        "expiration_date_iso": "2027-12-31",
                        "delivered_quantity": 1.0,
                    }
                )
                serial_index += 1

            summary_by_product.append(
                {
                    "product_code": product_code,
                    "confirmed_quantity": quantity,
                    "delivered_quantity_sum": float(quantity),
                    "serial_numbers": product_serials,
                }
            )

        return {
            "source_pdf": "factory-confirmation-79.pdf",
            "factory_document": {
                "bon_de_commande": "152870",
                "shipping_date_only_iso": "2026-07-16",
                "total_completed": 31,
            },
            "serial_items": serial_items,
            "summary_by_product": summary_by_product,
            "debug": {
                "page_count": 2,
                "pages_text_length": [],
            },
            "warnings": [],
            "summary": {
                "bon_de_commande": "152870",
                "serial_item_count": 31,
                "product_type_count": 10,
                "total_completed": 31,
            },
        }

    def run_finalize(self, factory_data=None):
        with patch(
            "factory_confirmations.services."
            "factory_confirmation_extraction_service."
            "sync_backorders_for_order"
        ), patch(
            "factory_confirmations.services."
            "factory_confirmation_extraction_service."
            "validate_document_workflow_item",
            return_value={},
        ):
            return finalize_factory_confirmation_after_order_match(
                confirmation=self.confirmation,
                user=self.user,
                factory_data=factory_data,
            )

    def run_extraction(self, factory_data):
        with patch(
            "factory_confirmations.services."
            "factory_confirmation_extraction_service."
            "extract_factory_confirmation",
            return_value=copy.deepcopy(factory_data),
        ), patch(
            "factory_confirmations.services."
            "factory_confirmation_extraction_service."
            "sync_backorders_for_order"
        ), patch(
            "factory_confirmations.services."
            "factory_confirmation_extraction_service."
            "validate_document_workflow_item",
            return_value={},
        ):
            return extract_factory_confirmation_for_confirmation(
                self.confirmation
            )

    def test_selected_order_processing_preserves_all_31_serial_items(self):
        factory_data = self.full_factory_data()
        original_identity = id(factory_data)

        selected_order = attach_order_from_factory_confirmation_data(
            confirmation=self.confirmation,
            factory_data=factory_data,
            pdf_path=Path(self.confirmation.confirmation_pdf.path),
        )

        self.assertEqual(selected_order, self.order)
        self.assertEqual(id(factory_data), original_identity)
        self.assertEqual(len(factory_data["serial_items"]), 31)
        self.assertEqual(len(factory_data["summary_by_product"]), 10)

    def test_extraction_saves_complete_data_and_creates_31_serial_items(self):
        result = self.run_extraction(self.full_factory_data())
        self.confirmation.refresh_from_db()

        required_keys = {
            "source_pdf",
            "factory_document",
            "serial_items",
            "summary_by_product",
            "debug",
            "warnings",
            "summary",
            "django",
        }
        self.assertTrue(required_keys.issubset(result))
        self.assertTrue(
            required_keys.issubset(
                self.confirmation.extracted_confirmation_data
            )
        )
        self.assertEqual(len(result["serial_items"]), 31)
        self.assertEqual(self.confirmation.serial_items.count(), 31)
        self.assertEqual(
            ShipmentBatchItem.objects.filter(
                batch__factory_confirmation=self.confirmation
            ).aggregate(total=Sum("shipped_quantity"))["total"],
            31,
        )
        self.assertEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.SUCCESS,
        )

        json_path = (
            get_factory_confirmation_workspace(self.confirmation)
            / "factory_confirmation.json"
        )
        saved_data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertTrue(required_keys.issubset(saved_data))
        self.assertEqual(len(saved_data["serial_items"]), 31)

    def test_django_metadata_update_does_not_replace_top_level_data(self):
        factory_data = self.full_factory_data()
        original_serial_items = factory_data["serial_items"]
        original_summary = factory_data["summary_by_product"]

        django_data = stamp_confirmation_debug_metadata(
            self.confirmation,
            factory_data,
        )
        django_data.update({"checkpoint": "preserved"})

        self.assertIs(factory_data["serial_items"], original_serial_items)
        self.assertIs(factory_data["summary_by_product"], original_summary)
        self.assertEqual(len(factory_data["serial_items"]), 31)
        self.assertEqual(factory_data["django"]["checkpoint"], "preserved")

    def test_finalize_from_complete_persisted_data_creates_31_serial_items(self):
        self.confirmation.extracted_confirmation_data = self.full_factory_data()
        self.confirmation.save(
            update_fields=["extracted_confirmation_data", "updated_at"]
        )

        self.run_finalize()
        self.confirmation.refresh_from_db()

        self.assertEqual(self.confirmation.serial_items.count(), 31)
        self.assertEqual(
            len(self.confirmation.extracted_confirmation_data["serial_items"]),
            31,
        )

    def test_empty_serial_items_do_not_create_shipment_or_workflow(self):
        invalid_data = self.full_factory_data()
        invalid_data["factory_document"]["total_completed"] = 0
        invalid_data["serial_items"] = []
        invalid_data["summary_by_product"] = []

        self.run_extraction(invalid_data)
        self.confirmation.refresh_from_db()

        self.assertFalse(
            ShipmentBatch.objects.filter(
                factory_confirmation=self.confirmation
            ).exists()
        )
        self.assertFalse(
            DocumentWorkflowItem.objects.filter(
                shipment_batch__factory_confirmation=self.confirmation
            ).exists()
        )
        self.assertEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )
        self.assertIn("没有提取到任何 Serial 明细", self.confirmation.extraction_error)

    def test_total_completed_31_with_no_serial_items_fails(self):
        invalid_data = self.full_factory_data()
        invalid_data["serial_items"] = []
        invalid_data["summary_by_product"] = []

        with self.assertRaises(FactoryExtractionValidationError):
            validate_factory_data_before_finalize(invalid_data)

        self.run_extraction(invalid_data)
        self.confirmation.refresh_from_db()
        self.assertEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )
        self.assertFalse(
            (self.confirmation.extracted_confirmation_data or {})
            .get("django", {})
            .get("finalized_after_order_match", False)
        )

    def test_validation_rejects_missing_fields_duplicates_and_bad_summary(self):
        mutations = [
            ("product_code", ""),
            ("serial_number", ""),
            ("expiration_date_iso", ""),
        ]
        for field, value in mutations:
            with self.subTest(field=field):
                invalid_data = self.full_factory_data()
                invalid_data["serial_items"][0][field] = value
                with self.assertRaises(FactoryExtractionValidationError):
                    validate_factory_data_before_finalize(invalid_data)

        duplicate_data = self.full_factory_data()
        duplicate_data["serial_items"][1]["serial_number"] = (
            duplicate_data["serial_items"][0]["serial_number"]
        )
        with self.assertRaises(FactoryExtractionValidationError):
            validate_factory_data_before_finalize(duplicate_data)

        summary_mismatch = self.full_factory_data()
        summary_mismatch["summary_by_product"][0]["confirmed_quantity"] = 3
        with self.assertRaises(FactoryExtractionValidationError):
            validate_factory_data_before_finalize(summary_mismatch)

    def test_failed_direct_finalize_does_not_mark_success_or_finalized(self):
        invalid_data = self.full_factory_data()
        invalid_data["serial_items"] = []
        invalid_data["summary_by_product"] = []
        self.confirmation.extracted_confirmation_data = invalid_data
        self.confirmation.save(
            update_fields=["extracted_confirmation_data", "updated_at"]
        )

        with self.assertRaises(FactoryExtractionValidationError):
            self.run_finalize()

        self.confirmation.refresh_from_db()
        self.assertEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )
        self.assertNotEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.SUCCESS,
        )
        self.assertFalse(
            self.confirmation.extracted_confirmation_data
            .get("django", {})
            .get("finalized_after_order_match", False)
        )
        self.assertFalse(
            ShipmentBatch.objects.filter(
                factory_confirmation=self.confirmation
            ).exists()
        )

    def test_failed_reextraction_preserves_existing_serial_and_batch_items(self):
        self.run_finalize(self.full_factory_data())
        batch = ShipmentBatch.objects.get(
            factory_confirmation=self.confirmation
        )
        workflow = DocumentWorkflowItem.objects.get(shipment_batch=batch)
        original_serial_ids = list(
            self.confirmation.serial_items
            .order_by("id")
            .values_list("id", flat=True)
        )
        original_batch_items = list(
            batch.shipped_items
            .order_by("id")
            .values_list("product_code", "shipped_quantity")
        )

        invalid_data = self.full_factory_data()
        invalid_data["serial_items"] = []
        invalid_data["summary_by_product"] = []
        self.run_extraction(invalid_data)

        self.confirmation.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(
            list(
                self.confirmation.serial_items
                .order_by("id")
                .values_list("id", flat=True)
            ),
            original_serial_ids,
        )
        self.assertEqual(
            list(
                batch.shipped_items
                .order_by("id")
                .values_list("product_code", "shipped_quantity")
            ),
            original_batch_items,
        )
        self.assertEqual(
            ShipmentBatch.objects.get(
                factory_confirmation=self.confirmation
            ).id,
            batch.id,
        )
        self.assertEqual(
            DocumentWorkflowItem.objects.get(shipment_batch=batch).id,
            workflow.id,
        )
        self.assertEqual(
            self.confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )

    def test_repeated_finalize_reuses_batch_and_workflow(self):
        factory_data = self.full_factory_data()

        first_batch, first_workflow, _result = self.run_finalize(
            factory_data
        )
        second_batch, second_workflow, _result = self.run_finalize(
            factory_data
        )

        self.assertEqual(second_batch.id, first_batch.id)
        self.assertEqual(second_workflow.id, first_workflow.id)
        self.assertEqual(
            ShipmentBatch.objects.filter(
                factory_confirmation=self.confirmation
            ).count(),
            1,
        )
        self.assertEqual(
            DocumentWorkflowItem.objects.filter(
                shipment_batch=first_batch
            ).count(),
            1,
        )
        self.assertEqual(self.confirmation.serial_items.count(), 31)
        self.assertEqual(
            ShipmentBatchItem.objects.filter(
                batch=first_batch
            ).aggregate(total=Sum("shipped_quantity"))["total"],
            31,
        )
