import copy
import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from documents.models import DocumentSequence, GeneratedDocument
from documents.services import historical_factory_po_repair_service as repair
from documents.services.document_numbering_service import audit_document_sequences
from orders.models import Order
from settlements.models import SettlementAccount
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


class HistoricalFactoryPORepairTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates/factory_purchase_order.html").write_text(
            "<html><title>{{ po.po_number }}</title><body>"
            "{{ po.po_number }} {{ po.order_date }} {{ po.expected_arrival }} "
            "{% for item in items %}{{ item.product_code }} {{ item.quantity }} "
            "{{ item.unit_price }} {{ item.discount }} {{ item.amount }}{% endfor %}"
            "{{ totals.total }}</body></html>"
        )
        self.override = override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.root / "media")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_user(username="po-repair")
        self.dump = self.root / "verified.dump"
        self.dump.write_bytes(b"PGDMP-test-fixture")
        self.writer = patch.object(repair, "write_html_and_pdf", side_effect=self.fake_writer)
        self.writer.start()
        self.addCleanup(self.writer.stop)

    def fake_writer(self, content, html, pdf, project_root):
        import fitz
        html.write_text(content)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((30, 30), repair.PO_TOKEN.search(content)[0])
        doc.save(str(pdf))
        doc.close()

    def fixture(self, bon="150222", sequence=5, old_sequence=1, batch_number=1):
        old_base = f"DELAHK{old_sequence:02d}05S"
        old = old_base + (f"-B{batch_number}" if batch_number > 1 else "")
        order = Order.objects.filter(bon_de_commande=bon).first()
        if order is None:
            order = Order.objects.create(
                bon_de_commande=bon, order_date=date(2026, 5, 29),
                hospital_name="Frozen hospital", created_by=self.user,
            )
            DocumentSequence.objects.create(
                bon_de_commande=bon, month_key="2026-05", sequence=sequence,
                invoice_number=f"Invoice 2026{sequence:02d}05", po_number=old_base,
            )
        batch = ShipmentBatch.objects.create(
            order=order, batch_number=batch_number, batch_date=date(2026, 6, 5), month_key="2026-06",
        )
        source = {
            "numbers": {"po_number": old, "base_po_number": old_base, "invoice_number": "Invoice HISTORICAL"},
            "po_data": {
                "po": {"po_number": old, "order_date": "29/05/2026", "expected_arrival": "08/06/2026"},
                "company": {"logo_path": "data/logo.png", "po_company": {}, "company_name": "Historic", "registration_no": "123"},
                "factory": {"factory_name": "Old Factory", "factory_address": ["Old address"], "buyer": "Original buyer"},
                "shipping_address": ["Frozen address"],
                "items": [{"product_code": "P1", "description": "Old product", "quantity": "2",
                           "unit_price": "100.00", "discount": "30%", "amount": "140.00",
                           "serial_numbers": ["SN1","SN2"], "expiration_dates": ["2026-12-31"]}],
                "totals": {"total": "140.00"},
                "debug": {"document_sequence": {"po_number": old, "base_po_number": old_base}},
            },
        }
        folder = self.root / "media" / bon / str(batch_number)
        folder.mkdir(parents=True)
        html, pdf = folder / f"Purchase_Order_{old}.html", folder / f"Purchase_Order_{old}.pdf"
        content = repair.render_po_html(source["po_data"], self.root / "templates/factory_purchase_order.html")
        self.fake_writer(content, html, pdf, self.root)
        html.with_name(html.stem + "_data.json").write_text(json.dumps(source))
        doc = GeneratedDocument.objects.create(
            order=order, shipment_batch=batch, document_type="factory_po", document_number=old,
            source_data=source, html_file=str(html.relative_to(self.root/"media")),
            pdf_file=str(pdf.relative_to(self.root/"media")), generated_by=self.user,
        )
        DocumentWorkflowItem.objects.create(order=order, shipment_batch=batch, po_document=doc)
        SettlementAccount.objects.create(document=doc, issue_date=date(2026,6,5), original_amount="140.00")
        return doc

    def apply(self):
        plan = repair.build_plan()
        return repair.apply_plan(plan, self.dump, plan["fingerprint"])

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def test_dry_run_writes_nothing(self):
        self.fixture()
        before_db, before_files = repair.database_state(), self.files()
        output = StringIO()
        call_command("repair_historical_factory_po_numbers", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["document_count"], 1)
        self.assertEqual(before_db, repair.database_state())
        self.assertEqual(before_files, self.files())

    def test_simple_repair_preserves_ids_fks_business_and_invoice_snapshots(self):
        doc = self.fixture()
        before = copy.deepcopy(doc.source_data)
        workflow = DocumentWorkflowItem.objects.get()
        settlement = SettlementAccount.objects.get()
        result = self.apply()
        doc.refresh_from_db()
        self.assertEqual(result["applied"], 1)
        self.assertEqual(doc.document_number, "DELAHK0505S")
        self.assertEqual(doc.source_data["numbers"]["invoice_number"], before["numbers"]["invoice_number"])
        for field in ("items", "totals", "factory", "shipping_address", "company"):
            self.assertEqual(doc.source_data["po_data"][field], before["po_data"][field])
        self.assertEqual(doc.source_data["po_data"]["po"]["order_date"], "29/05/2026")
        workflow.refresh_from_db()
        settlement.refresh_from_db()
        self.assertEqual(workflow.po_document_id, doc.pk)
        self.assertEqual(settlement.document_id, doc.pk)
        self.assertEqual(str(settlement.original_amount), "140.00")
        self.assertFalse(list(audit_document_sequences()))
        self.assertFalse(GeneratedDocument.objects.filter(document_number__startswith=repair.PREFIX).exists())
        self.assertTrue(Path(doc.pdf_file.path).exists())

    def test_swap_uses_temporary_namespace(self):
        a = self.fixture("150222", 5, 1)
        b = self.fixture("148859", 1, 5)
        with patch.object(repair.GeneratedDocument.objects, "create", side_effect=AssertionError("never recreate")):
            self.apply()
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.document_number, "DELAHK0505S")
        self.assertEqual(b.document_number, "DELAHK0105S")
        self.assertEqual(GeneratedDocument.objects.count(), 2)

    def test_chain_collision(self):
        docs = [self.fixture(str(150000+n), n+1, n) for n in (1,2,3)]
        self.apply()
        for index, doc in enumerate(docs, 2):
            doc.refresh_from_db()
            self.assertEqual(doc.document_number, f"DELAHK{index:02d}05S")
        self.assertFalse(list(audit_document_sequences()))

    def test_150222_batch_one_and_two_regression(self):
        first = self.fixture("150222", 5, 1)
        second = self.fixture("150222", 5, 1, 2)
        self.fixture("148859", 1, 5)
        self.apply()
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(first.document_number, "DELAHK0505S")
        self.assertEqual(second.document_number, "DELAHK0505S-B2")
        self.assertEqual(second.source_data["numbers"]["base_po_number"], "DELAHK0505S")
        seq = DocumentSequence.objects.get(bon_de_commande="150222")
        self.assertEqual((seq.sequence,seq.invoice_number,seq.po_number),(5,"Invoice 20260505","DELAHK0505S"))

    def test_insufficient_snapshot_aborts_without_live_rebuild(self):
        doc = self.fixture()
        doc.source_data = {}
        doc.save(update_fields=["source_data"])
        before = self.files()
        with self.assertRaisesMessage(repair.RepairError, "missing frozen po_data"):
            self.apply()
        self.assertEqual(before,self.files())

    def test_changed_audit_aborts(self):
        doc = self.fixture()
        plan = repair.build_plan()
        doc.notes = "concurrent change"
        doc.save(update_fields=["notes"])
        with self.assertRaisesMessage(repair.RepairError, "changed during staging"):
            repair.apply_plan(plan,self.dump,plan["fingerprint"])
        doc.refresh_from_db()
        self.assertEqual(doc.document_number, "DELAHK0105S")
        self.assertEqual(doc.notes, "concurrent change")

    def test_render_failure_keeps_originals(self):
        self.fixture()
        before_db = repair.database_state()
        before_files = {p:v for p,v in self.files().items() if p.startswith("media/")}
        with patch.object(repair,"write_html_and_pdf",side_effect=RuntimeError("renderer failed")):
            with self.assertRaisesMessage(RuntimeError,"renderer failed"):
                self.apply()
        self.assertEqual(before_db, repair.database_state())
        self.assertEqual(before_files,{p:v for p,v in self.files().items() if p.startswith("media/")})

    def test_transaction_failure_restores_published_files_and_database(self):
        self.fixture()
        before_db = repair.database_state()
        before_files = {p:v for p,v in self.files().items() if p.startswith("media/")}
        with patch.object(repair,"verify_result",side_effect=RuntimeError("verification failed")):
            with self.assertRaisesMessage(RuntimeError,"verification failed"):
                self.apply()
        self.assertEqual(before_db, repair.database_state())
        self.assertEqual(before_files,{p:v for p,v in self.files().items() if p.startswith("media/")})
        self.assertFalse(GeneratedDocument.objects.filter(document_number__startswith=repair.PREFIX).exists())

    def test_cleanup_failure_rolls_back_database_and_files(self):
        self.fixture()
        original_unlink = Path.unlink
        fail = [True]
        def fail_once(path, *args, **kwargs):
            if "media" in path.parts and fail[0]:
                fail[0] = False
                raise OSError("cleanup failure")
            return original_unlink(path,*args,**kwargs)
        before = repair.database_state()
        with patch.object(Path,"unlink",fail_once), self.assertRaisesMessage(OSError,"cleanup failure"):
            self.apply()
        self.assertEqual(before,repair.database_state())
        for row in GeneratedDocument.objects.all():
            self.assertTrue(Path(row.pdf_file.path).exists())
            self.assertTrue(Path(row.html_file.path).exists())

    def test_occupied_external_file_blocks(self):
        doc = self.fixture()
        target = Path(doc.pdf_file.path).with_name("Purchase_Order_DELAHK0505S.pdf")
        target.write_bytes(b"external")
        with self.assertRaisesMessage(repair.RepairError,"Occupied target path"):
            repair.build_plan()

    def test_backup_confirmation_required_by_command(self):
        self.fixture()
        with self.assertRaisesMessage(CommandError,"--confirm-database-backup"):
            call_command("repair_historical_factory_po_numbers", apply=True)

    def test_bad_fingerprint_aborts_before_writes(self):
        self.fixture()
        plan = repair.build_plan()
        before=self.files()
        with self.assertRaisesMessage(repair.RepairError,"changed since reviewed"):
            repair.apply_plan(plan,self.dump,"wrong")
        self.assertEqual(before,self.files())

    def test_already_consistent_order_not_in_repair_set(self):
        self.fixture(sequence=1,old_sequence=1)
        self.assertEqual(repair.build_plan()["documents"],[])

    def test_auxiliary_number_reference_only_changes_text(self):
        doc = self.fixture()
        account = SettlementAccount.objects.get(document=doc)
        account.notes = "PO DELAHK0105S"
        account.save(update_fields=["notes"])
        self.apply()
        account.refresh_from_db()
        self.assertEqual(account.notes, "PO DELAHK0505S")
        self.assertEqual(str(account.original_amount), "140.00")

    def test_source_patch_does_not_modify_arbitrary_business_values(self):
        original = {"numbers":{"po_number":"DELAHK0105S","base_po_number":"DELAHK0105S"},
                    "business":{"price":"20.00","date":"2026-05-29","quantity":3},
                    "invoice_number":"Invoice 20260105"}
        result, changes=repair.patch_po_snapshot(original,"DELAHK0105S","DELAHK0105S","DELAHK0505S","DELAHK0505S")
        self.assertEqual(result["business"],original["business"])
        self.assertEqual(result["invoice_number"],original["invoice_number"])
        self.assertEqual(len(changes),2)
