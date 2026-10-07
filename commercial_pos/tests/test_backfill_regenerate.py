import hashlib
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from commercial_pos.models import CommercialPOPricePolicy
from commercial_pos.services.generation_service import ensure_commercial_po_for_batch
from commercial_pos.services.price_service import CommercialPOError
from documents.models import DocumentSequence, GeneratedDocument
from factory_confirmations.models import FactoryConfirmation, SerialItem
from finance.services.finance_analysis_service import build_finance_dashboard_data
from finance.services.settlement_finance_service import build_settlement_finance_dashboard_data
from orders.models import Order, OrderItem
from pricing.models import PricePolicy
from settlements.models import PaymentTransaction, SettlementAccount
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_document_generation_service import generate_documents_for_workflow_item

from .fixtures import FixtureMixin, fake_legacy_writer, fake_render, make_factory_po


class CommercialBackfillRegenerateTests(FixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.batch_item.shipped_quantity = 53
        self.batch_item.save()
        self.order_item.requested_quantity = self.order_item.confirmed_quantity = 53
        self.order_item.save()
        self.serial.raw_data = {"delivered_quantity": 1}
        self.serial.save()
        for number in range(2, 54):
            SerialItem.objects.create(order=self.order, factory_confirmation=self.batch.factory_confirmation,
                product=self.order_item.product, product_code=self.order_item.product_code,
                serial_number=f"RERENDER-147891-{number}", expiration_date=date(2028, 1, 1))
        with patch("workflow.services.workflow_document_generation_service.write_invoice_html_and_pdf", side_effect=fake_legacy_writer), patch(
            "workflow.services.workflow_document_generation_service.write_po_html_and_pdf", side_effect=fake_legacy_writer
        ), patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            generate_documents_for_workflow_item(self.workflow, self.user)
        self.document = GeneratedDocument.objects.get(shipment_batch=self.batch, document_type="commercial_po")
        self.actor = get_user_model().objects.create_user(username="rerender-actor", is_staff=True)

    def row(self):
        return GeneratedDocument.objects.filter(pk=self.document.pk).values().get()

    def files(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in Path(self.media.name).rglob("*") if path.is_file()}

    def protected_state(self):
        models = (Order, OrderItem, ShipmentBatch, ShipmentBatchItem, FactoryConfirmation, SerialItem,
                  PricePolicy, CommercialPOPricePolicy, DocumentSequence, DocumentWorkflowItem, SettlementAccount, PaymentTransaction)
        state = {model._meta.label: list(model.objects.order_by("pk").values()) for model in models}
        state["old_documents"] = list(GeneratedDocument.objects.exclude(document_type="commercial_po").order_by("pk").values())
        state["finance"] = build_finance_dashboard_data()
        state["settlement_finance"] = build_settlement_finance_dashboard_data()
        state["settlement_finance"].pop("generated_at", None)
        return state

    def command(self, **kwargs):
        out = StringIO()
        call_command("backfill_commercial_pos", bon=["147891"], stdout=out, **kwargs)
        return out.getvalue()

    def test_default_still_skips_existing_with_and_without_apply(self):
        before, files, state = self.row(), self.files(), self.protected_state()
        with patch("commercial_pos.services.generation_service.render_commercial_files") as render:
            for options in ({}, {"apply": True, "actor_id": self.actor.pk}):
                self.assertIn("SKIP", self.command(**options))
            render.assert_not_called()
        self.assertEqual(self.row(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(self.protected_state(), state)

    def test_regenerate_dry_run_is_explicit_and_has_zero_writes_or_rendering(self):
        before, files, state = self.row(), self.files(), self.protected_state()
        with patch("commercial_pos.services.generation_service.render_commercial_files") as render, CaptureQueriesContext(connection) as queries:
            output = self.command(regenerate_existing=True)
        for text in ("DRY-RUN", "WOULD REGENERATE", "BON=147891", "Batch=1", "document=CPO-147891-B1", "EUR=8480.00"):
            self.assertIn(text, output)
        render.assert_not_called()
        self.assertFalse([q for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(self.row(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(self.protected_state(), state)

    def test_apply_reuses_identity_renders_current_template_and_preserves_old_system(self):
        before, files, state = self.row(), self.files(), self.protected_state()
        count = GeneratedDocument.objects.count()
        output = self.command(regenerate_existing=True, apply=True, actor_id=self.actor.pk)
        self.assertIn("REGENERATED", output)
        self.assertIn("regenerated=1", output)
        after = self.row()
        for field in ("id", "document_number", "shipment_batch_id", "order_id", "document_type", "notes"):
            self.assertEqual(after[field], before[field])
        for field in ("pdf_file", "html_file", "source_data", "generated_by_id", "generated_at"):
            self.assertNotEqual(after[field], before[field])
        self.assertEqual(after["generated_by_id"], self.actor.pk)
        self.assertEqual(after["source_data"]["shipping_date"], "2026-04-22")
        self.assertEqual(after["source_data"]["unit_price"], "160.00")
        self.assertEqual(after["source_data"]["total_units"], "53")
        self.assertEqual(after["source_data"]["total_amount"], "8480.00")
        self.assertEqual(after["source_data"]["rendering"]["template"], "templates/factory_purchase_order.html")
        html = (Path(self.media.name) / after["html_file"]).read_text()
        self.assertIn('class="po-items-table"', html)
        self.assertIn("8,480.00", html)
        self.assertIn(after["source_data"]["po_data"]["po"]["po_number"], html)
        self.assertNotIn("CPO-147891-B1", html)
        self.assertTrue((Path(self.media.name) / after["pdf_file"]).read_bytes().startswith(b"%PDF-"))
        self.assertEqual(GeneratedDocument.objects.count(), count)
        self.assertEqual(self.protected_state(), state)
        for path, digest in files.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)

    def test_regeneration_requires_a_unique_current_commercial_rule(self):
        before, files = self.row(), self.files()
        CommercialPOPricePolicy.objects.update(is_active=False)
        with self.assertRaises(CommercialPOError):
            ensure_commercial_po_for_batch(self.batch.pk, self.actor, regenerate_existing=True)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.files(), files)

    def test_all_and_unscoped_regeneration_are_rejected_before_any_write(self):
        before, files = self.row(), self.files()
        for options in ({"all": True}, {"all": True, "apply": True, "actor_id": self.actor.pk}, {}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command("backfill_commercial_pos", regenerate_existing=True, stdout=StringIO(), **options)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.files(), files)

    def test_apply_actor_rules_stay_unchanged(self):
        self.actor.groups.add(Group.objects.get_or_create(name="Hospital Demo")[0])
        for actor_id in (None, self.actor.pk, 999999):
            with self.subTest(actor_id=actor_id), self.assertRaises(CommandError):
                self.command(regenerate_existing=True, apply=True, actor_id=actor_id)

    def test_missing_batch_remains_normal_backfill_with_regenerate_flag(self):
        from .fixtures import make_batch
        _, batch, _, _, _, _ = make_batch(self.user, bon="148002", quantity=1)
        make_factory_po(self.user, batch)
        dry = StringIO()
        call_command("backfill_commercial_pos", batch_id=[batch.pk], regenerate_existing=True, stdout=dry)
        self.assertIn("eligible=1", dry.getvalue())
        self.assertNotIn("WOULD REGENERATE", dry.getvalue())
        self.assertFalse(GeneratedDocument.objects.filter(shipment_batch=batch, document_type="commercial_po").exists())
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            call_command("backfill_commercial_pos", batch_id=[batch.pk], regenerate_existing=True,
                         apply=True, actor_id=self.actor.pk, stdout=StringIO())
        self.assertEqual(GeneratedDocument.objects.filter(shipment_batch=batch, document_type="commercial_po").count(), 1)
        self.assertEqual(GeneratedDocument.objects.filter(shipment_batch=batch, document_type="factory_po").count(), 1)

    def test_render_failure_keeps_old_record_files_and_snapshot(self):
        before, files, state = self.row(), self.files(), self.protected_state()
        def partial_render(snapshot, directory):
            (directory / "document.html").write_text("unfinished")
            raise RuntimeError("synthetic rendering failure")
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=partial_render), self.assertLogs(
            "commercial_pos.management.commands.backfill_commercial_pos", level="ERROR"
        ), self.assertRaises(CommandError):
            self.command(regenerate_existing=True, apply=True, actor_id=self.actor.pk)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(self.protected_state(), state)
        self.assertFalse(list(Path(self.media.name).rglob(".pending-*")))

    def test_database_update_failure_keeps_old_pointers_and_all_old_files(self):
        before, files = self.row(), self.files()
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render), patch(
            "django.db.models.query.QuerySet.update", side_effect=RuntimeError("synthetic database failure")
        ), self.assertLogs("commercial_pos.services.generation_service", level="WARNING"), self.assertRaises(RuntimeError):
            ensure_commercial_po_for_batch(self.batch.pk, self.actor, regenerate_existing=True)
        self.assertEqual(self.row(), before)
        for path, digest in files.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)

    def test_explicit_regeneration_repairs_old_payload_from_frozen_factory_authority(self):
        GeneratedDocument.objects.filter(pk=self.document.pk).update(source_data={"damaged": True})
        before, files = self.row(), self.files()
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=fake_render):
            ensure_commercial_po_for_batch(self.batch.pk, self.actor, regenerate_existing=True)
        after = self.row()
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["document_number"], before["document_number"])
        self.assertEqual(after["source_data"]["total_amount"], "8480.00")
        for path, digest in files.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
    def test_concurrent_regeneration_reuses_one_record_and_rejects_stale_publication(self):
        barrier = Barrier(2)
        def render(snapshot, directory):
            fake_render(snapshot, directory)
            barrier.wait(timeout=20)
        def regenerate():
            close_old_connections()
            try:
                return ensure_commercial_po_for_batch(self.batch.pk, self.actor, regenerate_existing=True).pk
            except CommercialPOError:
                return "stale"
            finally:
                close_old_connections()
        with patch("commercial_pos.services.generation_service.render_commercial_files", side_effect=render), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: regenerate(), range(2)))
        self.assertCountEqual(outcomes, [self.document.pk, "stale"])
        self.assertEqual(GeneratedDocument.objects.filter(shipment_batch=self.batch, document_type="commercial_po").count(), 1)
        self.document.refresh_from_db()
        self.assertTrue(Path(self.document.pdf_file.path).is_file())
        self.assertTrue(Path(self.document.html_file.path).is_file())
