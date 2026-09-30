from datetime import date
from django.test import TestCase
from factory_confirmations import test_extraction_data_integrity as fixtures
from factory_confirmations.models import FactoryConfirmation
from shipments.models import ShipmentBatch
from shipments.services.shipment_tracking_service import sync_shipment_batch_from_factory_confirmation
from workflow.models import DocumentWorkflowItem


class ExplicitBatchReferenceTests(TestCase):
    setUp = fixtures.FactoryConfirmationDataIntegrityTests.setUp
    full_factory_data = fixtures.FactoryConfirmationDataIntegrityTests.full_factory_data
    run_extraction = fixtures.FactoryConfirmationDataIntegrityTests.run_extraction
    run_finalize = fixtures.FactoryConfirmationDataIntegrityTests.run_finalize

    def set_reference(self, suffix=""):
        self.order.bon_de_commande = "155141"
        self.order.save(update_fields=["bon_de_commande"])
        data = self.full_factory_data()
        data["factory_document"]["bon_de_commande"] = "155141" + suffix
        data["summary"]["bon_de_commande"] = "155141"
        return data

    def prior_batch(self, number, confirmation=None):
        return ShipmentBatch.objects.create(
            order=self.order, batch_number=number,
            factory_confirmation=confirmation, batch_date=date(2026, 9, 20),
            month_key="2026-09",
        )

    def assert_blocked(self, data, message):
        before = list(ShipmentBatch.objects.values_list("pk", "batch_number"))
        self.run_extraction(data)
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.extraction_status, "failed")
        self.assertIn(message, self.confirmation.extraction_error)
        self.assertEqual(list(ShipmentBatch.objects.values_list("pk", "batch_number")), before)

    def test_b2_auto_matches_base_order_and_workflow(self):
        self.prior_batch(1)
        data = self.set_reference("-B2")
        self.confirmation.order = None
        self.confirmation.save(update_fields=["order"])
        result = self.run_extraction(data)
        self.confirmation.refresh_from_db()
        batch = ShipmentBatch.objects.get(factory_confirmation=self.confirmation)
        self.assertEqual(self.confirmation.order_id, self.order.pk)
        self.assertEqual(self.confirmation.confirmation_type, "replenishment")
        self.assertEqual(batch.batch_number, 2)
        self.assertEqual(result["django"]["detected_order_reference"], "155141-B2")
        self.assertEqual(result["django"]["detected_batch_source"], "factory_order_reference_suffix")
        self.assertTrue(DocumentWorkflowItem.objects.filter(shipment_batch=batch).exists())

    def test_b2_without_b1(self):
        self.assert_blocked(self.set_reference("-B2"), "尚不存在 Batch 1")
        self.assertFalse(self.confirmation.serial_items.exists())

    def test_b3_gap(self):
        self.prior_batch(1)
        self.assert_blocked(self.set_reference("-B3"), "尚不存在 Batch 2")

    def test_b3_with_predecessors(self):
        self.prior_batch(1)
        self.prior_batch(2)
        self.run_extraction(self.set_reference("-B3"))
        self.assertEqual(ShipmentBatch.objects.get(factory_confirmation=self.confirmation).batch_number, 3)

    def test_duplicate_b2_owned_by_other_confirmation(self):
        self.prior_batch(1)
        other = FactoryConfirmation.objects.create(order=self.order, created_by=self.user)
        self.prior_batch(2, other)
        self.assert_blocked(self.set_reference("-B2"), "请检查是否重复上传")

    def test_reextraction_reuses_same_batch_and_workflow(self):
        self.prior_batch(1)
        data = self.set_reference("-B2")
        self.run_extraction(data)
        batch = ShipmentBatch.objects.get(factory_confirmation=self.confirmation)
        workflow = DocumentWorkflowItem.objects.get(shipment_batch=batch)
        self.run_extraction(data)
        self.assertEqual(ShipmentBatch.objects.get(factory_confirmation=self.confirmation).pk, batch.pk)
        self.assertEqual(DocumentWorkflowItem.objects.get(shipment_batch=batch).pk, workflow.pk)
        self.assertEqual(ShipmentBatch.objects.count(), 2)

    def test_reextraction_mismatch_preserves_identity_and_serials(self):
        self.prior_batch(1)
        self.run_extraction(self.set_reference("-B2"))
        serials = list(self.confirmation.serial_items.values_list("pk", flat=True))
        self.assert_blocked(self.set_reference("-B3"), "已经对应 Batch 2")
        self.assertEqual(list(self.confirmation.serial_items.values_list("pk", flat=True)), serials)

    def test_legacy_first_batch_auto(self):
        self.run_extraction(self.set_reference())
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.confirmation_type, "initial")
        self.assertEqual(ShipmentBatch.objects.get(factory_confirmation=self.confirmation).batch_number, 1)

    def test_legacy_existing_batches_manual_replenishment(self):
        self.prior_batch(1)
        self.confirmation.confirmation_type = "replenishment"
        self.confirmation.save(update_fields=["confirmation_type"])
        self.run_extraction(self.set_reference())
        self.assertEqual(ShipmentBatch.objects.get(factory_confirmation=self.confirmation).batch_number, 2)

    def test_legacy_auto_ambiguity_retains_extracted_data_for_recovery(self):
        self.prior_batch(1)
        self.confirmation.extracted_confirmation_data = {"django": {"batch_selection_mode": "auto"}}
        self.confirmation.save(update_fields=["extracted_confirmation_data"])
        self.assert_blocked(self.set_reference(), "请人工选择")
        self.assertTrue(self.confirmation.extracted_confirmation_data["serial_items"])
        self.assertTrue(self.confirmation.extracted_confirmation_data["django"]["requires_batch_selection"])

    def test_explicit_reference_does_not_bypass_manual_order_mismatch(self):
        self.prior_batch(1)
        data = self.set_reference("-B2")
        self.order.bon_de_commande = "155999"
        self.order.save(update_fields=["bon_de_commande"])
        self.run_extraction(data)
        self.confirmation.refresh_from_db()
        self.assertTrue(self.confirmation.extracted_confirmation_data["django"]["requires_manual_confirmation"])
        self.assertFalse(ShipmentBatch.objects.filter(factory_confirmation=self.confirmation).exists())

    def test_sync_service_itself_rejects_gap(self):
        self.confirmation.shipping_date = date(2026, 9, 22)
        with self.assertRaisesMessage(ValueError, "尚不存在 Batch 1"):
            sync_shipment_batch_from_factory_confirmation(self.confirmation, explicit_batch_number=2)
        self.assertFalse(ShipmentBatch.objects.exists())

    def test_pdf_suffix_wins_over_legacy_base_only_metadata(self):
        import fitz
        data = self.set_reference()
        self.prior_batch(1)
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Order: 155141-B2")
            document.save(self.confirmation.confirmation_pdf.path)
        result = self.run_extraction(data)
        self.assertEqual(result["django"]["detected_batch_number"], 2)
        self.assertEqual(ShipmentBatch.objects.get(factory_confirmation=self.confirmation).batch_number, 2)

    def test_legacy_reextraction_preserves_historical_batch_even_with_gap(self):
        own = self.prior_batch(2, self.confirmation)
        self.run_extraction(self.set_reference())
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.extraction_status, "success")
        own.refresh_from_db()
        self.assertEqual(own.batch_number, 2)
        self.assertEqual(ShipmentBatch.objects.count(), 1)

    def test_explicit_b1_cannot_duplicate_existing_first_batch(self):
        self.prior_batch(1)
        self.assert_blocked(self.set_reference("-B1"), "已经存在 Batch 1")
