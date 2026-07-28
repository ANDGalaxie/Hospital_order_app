from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from backorders.models import BackorderLine, BackorderOrderFolder
from factories.models import Factory
from factory_confirmations.models import FactoryConfirmation
from orders.models import Order, OrderItem
from portal.services.factory_portal_service import get_shipment_batch
from pricing.models import PricePolicy
from products.models import Product, ProductCategory
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class FactoryUploadPortalFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="portal-staff",
            password="test",
            is_staff=True,
        )
        self.client.force_login(self.user)

        self.factory = Factory.objects.create(
            name="Portal Factory",
            short_name="PF",
        )
        self.department = ProductCategory.objects.create(
            name="Portal Department",
            node_type=ProductCategory.NodeType.DEPARTMENT,
        )
        self.factory_node = ProductCategory.objects.create(
            name="Portal Factory Node",
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=self.factory,
        )
        self.category = ProductCategory.objects.create(
            name="Portal Category",
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
        )
        self.product = Product.objects.create(
            code="PORTAL-001",
            description="Portal Product",
            category=self.category,
            factory=self.factory,
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
        )
        self.policy = PricePolicy.objects.create(
            name="Portal Policy",
            factory=self.factory,
            category=self.category,
            start_date=date(2026, 1, 1),
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
            expiration_discount_rate=Decimal("0.10"),
            expiration_threshold_days=365,
            is_active=True,
        )
        self.order = self.create_order("ORDER-001")

    def create_order(self, bon):
        order = Order.objects.create(
            bon_de_commande=bon,
            hospital_name="Portal Hospital",
            hospital_order_pdf="hospital_orders/test.pdf",
            factory=self.factory,
            created_by=self.user,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_code=self.product.code,
            requested_quantity=2,
            confirmed_quantity=0,
            backordered_quantity=2,
            hospital_unit_price=Decimal("50.00"),
            factory_unit_price=Decimal("20.00"),
            expiration_discount_rate=Decimal("0.10"),
            expiration_threshold_days=365,
            price_policy=self.policy,
            price_policy_date=date(2026, 6, 1),
        )
        folder = BackorderOrderFolder.objects.create(
            order=order,
            line_count=1,
            remaining_total_quantity=2,
        )
        BackorderLine.objects.create(
            order_folder=folder,
            order=order,
            product=self.product,
            product_code=self.product.code,
            description=self.product.description,
            requested_quantity=2,
            shipped_quantity=0,
            remaining_quantity=2,
        )
        return order

    def uploaded_pdf(self, name="confirmation.pdf"):
        return SimpleUploadedFile(
            name,
            b"%PDF-1.4 test pdf",
            content_type="application/pdf",
        )

    def confirmation_payload(self, **overrides):
        payload = {
            "confirmation_pdf": self.uploaded_pdf(),
            "order_id": str(self.order.id),
            "factory_id": str(self.factory.id),
            "confirmation_type": FactoryConfirmation.ConfirmationType.INITIAL,
            "bon_de_commande_manual_note": "",
        }
        payload.update(overrides)
        return payload

    def extractor_result(self, detected_bon=None):
        result = {
            "factory_document": {
                "shipping_date_only_iso": "2026-07-16",
            },
            "serial_items": [
                {
                    "product_code": self.product.code,
                    "serial_number": "SERIAL-001",
                    "expiration_date_iso": "2027-07-16",
                },
                {
                    "product_code": self.product.code,
                    "serial_number": "SERIAL-002",
                    "expiration_date_iso": "2027-07-17",
                },
            ],
            "summary": {},
            "warnings": [],
        }
        if detected_bon is not None:
            result["summary"]["bon_de_commande"] = detected_bon
        return result

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_upload_initial_factory_confirmation_from_order(self, extract_mock, _sync_mock):
        extract_mock.return_value = self.extractor_result(detected_bon=None)

        response = self.client.post(
            reverse("portal:order_factory_upload", args=[self.order.id]) + "?type=initial",
            self.confirmation_payload(),
        )

        self.assertEqual(response.status_code, 302)
        confirmation = FactoryConfirmation.objects.get(order=self.order)
        self.assertEqual(
            confirmation.confirmation_type,
            FactoryConfirmation.ConfirmationType.INITIAL,
        )
        self.assertIsNotNone(get_shipment_batch(confirmation))
        self.assertTrue(
            DocumentWorkflowItem.objects.filter(
                shipment_batch__factory_confirmation=confirmation,
            ).exists()
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_upload_replenishment_from_existing_order_batch(self, extract_mock, _sync_mock):
        extract_mock.return_value = self.extractor_result(detected_bon=None)
        ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=1,
            batch_date=date(2026, 7, 1),
            month_key="2026-07",
        )

        response = self.client.post(
            reverse("portal:order_factory_upload", args=[self.order.id]) + "?type=replenishment",
            self.confirmation_payload(
                confirmation_type=FactoryConfirmation.ConfirmationType.REPLENISHMENT
            ),
        )

        self.assertEqual(response.status_code, 302)
        confirmation = FactoryConfirmation.objects.order_by("-id").first()
        self.assertEqual(
            confirmation.confirmation_type,
            FactoryConfirmation.ConfirmationType.REPLENISHMENT,
        )
        self.assertTrue(
            ShipmentBatch.objects.filter(factory_confirmation=confirmation).exists()
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_selected_order_without_bon_keeps_processing(self, extract_mock, _sync_mock):
        extract_mock.return_value = self.extractor_result(detected_bon=None)

        self.client.post(
            reverse("portal:factory_upload"),
            self.confirmation_payload(),
        )

        confirmation = FactoryConfirmation.objects.latest("id")
        self.assertEqual(confirmation.order_id, self.order.id)
        self.assertTrue(
            ShipmentBatch.objects.filter(factory_confirmation=confirmation).exists()
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_selected_order_mismatch_with_manual_confirmation_continues(self, extract_mock, _sync_mock):
        extract_mock.return_value = self.extractor_result(detected_bon="999999")

        self.client.post(
            reverse("portal:factory_upload"),
            self.confirmation_payload(
                bon_de_commande_manual_confirmed="1",
                bon_de_commande_manual_note="manual ok",
            ),
        )

        confirmation = FactoryConfirmation.objects.latest("id")
        self.assertTrue(confirmation.bon_de_commande_manual_confirmed)
        self.assertTrue(
            ShipmentBatch.objects.filter(factory_confirmation=confirmation).exists()
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_selected_order_mismatch_without_confirmation_blocks_finalize(self, extract_mock, _sync_mock):
        extract_mock.return_value = self.extractor_result(detected_bon="999999")

        self.client.post(
            reverse("portal:factory_upload"),
            self.confirmation_payload(),
        )

        confirmation = FactoryConfirmation.objects.latest("id")
        self.assertEqual(
            confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )
        self.assertFalse(
            ShipmentBatch.objects.filter(factory_confirmation=confirmation).exists()
        )
        self.assertIn(
            "需要人工确认",
            confirmation.extraction_error,
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_unmatched_order_keeps_extracted_data(self, extract_mock):
        extract_mock.return_value = self.extractor_result(detected_bon="404404")

        self.client.post(
            reverse("portal:factory_upload"),
            {
                "confirmation_pdf": self.uploaded_pdf(),
                "confirmation_type": FactoryConfirmation.ConfirmationType.INITIAL,
                "factory_id": str(self.factory.id),
            },
        )

        confirmation = FactoryConfirmation.objects.latest("id")
        self.assertIsNone(confirmation.order_id)
        self.assertTrue(confirmation.extracted_confirmation_data)
        self.assertEqual(
            confirmation.extracted_confirmation_data["django"]["detected_bon_de_commande"],
            "404404",
        )

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation", side_effect=AssertionError("OCR should not rerun"))
    @patch("factory_confirmations.services.factory_confirmation_extraction_service.sync_backorders_for_order")
    def test_manual_associate_after_unmatched_uses_existing_ocr(self, _sync_mock, extract_mock):
        confirmation = FactoryConfirmation.objects.create(
            confirmation_type=FactoryConfirmation.ConfirmationType.INITIAL,
            factory=self.factory,
            confirmation_pdf=self.uploaded_pdf("manual.pdf"),
            created_by=self.user,
            extracted_confirmation_data=self.extractor_result(detected_bon="404404"),
            extraction_status=FactoryConfirmation.ExtractionStatus.FAILED,
            extraction_error="waiting for match",
        )

        response = self.client.post(
            reverse("portal:factory_action", args=[confirmation.id]),
            {
                "action": "associate_order",
                "order_id": str(self.order.id),
                "bon_de_commande_manual_confirmed": "1",
                "bon_de_commande_manual_note": "matched later",
            },
        )

        self.assertEqual(response.status_code, 302)
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.order_id, self.order.id)
        self.assertTrue(
            ShipmentBatch.objects.filter(factory_confirmation=confirmation).exists()
        )
        extract_mock.assert_not_called()

    @patch("factory_confirmations.services.factory_confirmation_extraction_service.extract_factory_confirmation")
    def test_initial_replenishment_sequence_validation_still_applies(self, extract_mock):
        extract_mock.return_value = self.extractor_result(detected_bon=None)
        ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=1,
            batch_date=date(2026, 7, 1),
            month_key="2026-07",
        )

        self.client.post(
            reverse("portal:factory_upload"),
            self.confirmation_payload(),
        )

        confirmation = FactoryConfirmation.objects.latest("id")
        self.assertEqual(
            confirmation.extraction_status,
            FactoryConfirmation.ExtractionStatus.FAILED,
        )
        self.assertIn("不能再选择“首批发货”", confirmation.extraction_error)

    def test_shipment_pages_are_accessible(self):
        batch = ShipmentBatch.objects.create(
            order=self.order,
            source_type=ShipmentBatch.SourceType.MANUAL,
            batch_number=1,
            batch_date=date(2026, 7, 1),
            month_key="2026-07",
        )

        response = self.client.get(reverse("portal:shipment_list"))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("portal:shipment_detail", args=[batch.id]))
        self.assertEqual(response.status_code, 200)

    def test_backorder_pages_are_accessible(self):
        line = BackorderLine.objects.filter(order=self.order).first()

        response = self.client.get(reverse("portal:backorder_list"))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("portal:backorder_detail", args=[line.id]))
        self.assertEqual(response.status_code, 200)
