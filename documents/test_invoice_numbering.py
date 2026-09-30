from datetime import date
from types import SimpleNamespace
from django.test import SimpleTestCase
from documents.services.document_numbering_service import build_expected_invoice_number, compute_expected_invoice_sequence

class InvoiceNumberingTests(SimpleTestCase):
    def order(self, ident, bon, day, created=1):
        return SimpleNamespace(id=ident, bon_de_commande=bon, order_date=day, created_at=created)
    def test_format_examples_keep_sequence_before_month(self):
        cases=[(date(2026,5,1),1,"Invoice 20260105"),(date(2026,5,5),5,"Invoice 20260505"),(date(2026,7,2),2,"Invoice 20260207"),(date(2026,1,5),5,"Invoice 20260501")]
        for day, sequence, expected in cases:
            orders=[self.order(i,str(i),day) for i in range(1,sequence+1)]
            self.assertEqual(build_expected_invoice_number(orders[-1],orders)["invoice_number"],expected)
        self.assertNotEqual(build_expected_invoice_number(self.order(1,"1",date(2026,5,1)),[self.order(1,"1",date(2026,5,1))])["invoice_number"],"Invoice 20260501")
    def test_order_date_and_numeric_bon_determine_rank(self):
        later=self.order(9,"BON 20",date(2026,5,20),created=1); earlier=self.order(1," 10 ",date(2026,5,1),created=999)
        self.assertEqual(compute_expected_invoice_sequence(earlier,[later,earlier]),1)
        self.assertEqual(compute_expected_invoice_sequence(later,[later,earlier]),2)
    def test_same_day_bon_and_id_are_stable(self):
        a=self.order(2,"BON X",date(2026,5,1)); b=self.order(1,"BON X",date(2026,5,1)); c=self.order(3,"BON 7",date(2026,5,1))
        self.assertEqual(compute_expected_invoice_sequence(c,[a,b,c]),1)
        self.assertEqual(compute_expected_invoice_sequence(b,[a,b,c]),2)
        self.assertEqual(compute_expected_invoice_sequence(a,[a,b,c]),3)
    def test_missing_date_blocks(self):
        order=self.order(1,"1",None)
        with self.assertRaisesRegex(ValueError,"missing order_date"):
            build_expected_invoice_number(order,[order])


from io import StringIO
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from documents.models import DocumentSequence
from documents.services.document_numbering_service import (
    DocumentSequenceConsistencyError,
    get_or_create_expected_invoice_numbers,
)
from orders.models import Order
from workflow.services.workflow_document_generation_service import get_batch_document_numbers


class FrozenDocumentSequenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="frozen-numbering")
        self.order = self.create_order("152376", date(2026, 7, 2))

    def create_order(self, bon, day):
        return Order.objects.create(
            bon_de_commande=bon, order_date=day, hospital_name="Numbering Test",
            hospital_order_pdf="orders/test.pdf", created_by=self.user,
        )

    def freeze(self, sequence=2, invoice="Invoice 20260207", po="DELAHK0207S"):
        return DocumentSequence.objects.create(
            bon_de_commande=self.order.bon_de_commande,
            month_key=self.order.order_date.strftime("%Y-%m"),
            sequence=sequence, invoice_number=invoice, po_number=po,
        )

    def use_may_order(self):
        self.order.bon_de_commande = "150222"
        self.order.order_date = date(2026, 5, 29)
        self.order.save(update_fields=["bon_de_commande", "order_date"])

    def test_frozen_sequence_wins_without_ranking_or_writing(self):
        record = self.freeze()
        self.assertEqual(compute_expected_invoice_sequence(self.order), 1)
        with patch(
            "documents.services.document_numbering_service.build_expected_invoice_number",
            side_effect=AssertionError("frozen order must not be ranked"),
        ), patch.object(DocumentSequence, "save", side_effect=AssertionError("no writes")):
            result = get_or_create_expected_invoice_numbers(self.order)
        self.assertEqual(result["sequence"], 2)
        self.assertEqual(result["invoice_number"], "Invoice 20260207")
        self.assertEqual(result["po_number"], "DELAHK0207S")
        self.assertEqual(result["sequence_id"], record.pk)
        self.assertFalse(result["created"])

    def test_frozen_batches_reuse_base_with_unchanged_suffix(self):
        self.freeze()
        for number, suffix in ((1, ""), (2, "-B2"), (3, "-B3")):
            batch = SimpleNamespace(order=self.order, batch_number=number, batch_date=date(2026, 9, 22))
            result = get_batch_document_numbers(batch)
            self.assertEqual(result["invoice_number"], "Invoice 20260207" + suffix)
            self.assertEqual(result["po_number"], "DELAHK0207S" + suffix)
            self.assertEqual(result["sequence"], 2)
        self.assertEqual(DocumentSequence.objects.count(), 1)

    def test_invoice_mismatch_blocks_without_filling_blank_po(self):
        record = self.freeze(invoice="Invoice 20260307", po="")
        with self.assertRaisesMessage(DocumentSequenceConsistencyError, "expected_invoice=Invoice 20260207"):
            get_or_create_expected_invoice_numbers(self.order)
        record.refresh_from_db()
        self.assertEqual(record.po_number, "")
        self.assertEqual(record.invoice_number, "Invoice 20260307")

    def test_nonblank_po_mismatch_blocks_without_overwrite(self):
        self.use_may_order()
        record = self.freeze(5, "Invoice 20260505", "DELAHK0105S")
        with self.assertRaisesMessage(DocumentSequenceConsistencyError, "expected_po=DELAHK0505S"):
            get_or_create_expected_invoice_numbers(self.order)
        record.refresh_from_db()
        self.assertEqual(record.po_number, "DELAHK0105S")
        self.assertEqual(record.sequence, 5)

    def test_blank_po_is_filled_from_frozen_sequence(self):
        self.use_may_order()
        record = self.freeze(5, "Invoice 20260505", "")
        result = get_or_create_expected_invoice_numbers(self.order)
        record.refresh_from_db()
        self.assertEqual(record.po_number, "DELAHK0505S")
        self.assertEqual(record.invoice_number, "Invoice 20260505")
        self.assertEqual(result["sequence_id"], record.pk)
        self.assertEqual(result["sequence"], 5)
        self.assertFalse(result["created"])
        self.assertEqual(DocumentSequence.objects.count(), 1)

    def test_correct_existing_may_sequence_has_no_update(self):
        self.use_may_order()
        self.freeze(5, "Invoice 20260505", "DELAHK0505S")
        with CaptureQueriesContext(connection) as queries:
            result = get_or_create_expected_invoice_numbers(self.order)
        self.assertEqual(result["sequence"], 5)
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for query in queries
        ))

    def test_new_order_still_uses_theoretical_rank(self):
        self.create_order("152300", date(2026, 7, 1))
        result = get_or_create_expected_invoice_numbers(self.order)
        self.assertTrue(result["created"])
        self.assertEqual(result["sequence"], 2)
        self.assertEqual(result["invoice_number"], "Invoice 20260207")
        self.assertEqual(result["po_number"], "DELAHK0207S")

    def test_new_order_keeps_historical_drift_protection(self):
        other = self.create_order("152300", date(2026, 7, 1))
        DocumentSequence.objects.create(
            bon_de_commande=other.bon_de_commande, month_key="2026-07",
            sequence=3, invoice_number="Invoice 20260307", po_number="DELAHK0307S",
        )
        with self.assertRaisesMessage(ValueError, "requires historical rebuild"):
            get_or_create_expected_invoice_numbers(self.order)
        self.assertFalse(DocumentSequence.objects.filter(bon_de_commande=self.order.bon_de_commande).exists())

    def test_missing_date_still_blocks(self):
        self.freeze()
        self.order.order_date = None
        with self.assertRaisesMessage(ValueError, "missing order_date"):
            get_or_create_expected_invoice_numbers(self.order)

    def test_zero_sequence_blocks(self):
        self.freeze(0, "Invoice 20260007", "")
        with self.assertRaises(DocumentSequenceConsistencyError):
            get_or_create_expected_invoice_numbers(self.order)

    def test_audit_reports_mismatch_and_blank_po_without_writes(self):
        self.freeze()  # Correct frozen exception must not be reported.
        mismatch = DocumentSequence.objects.create(
            bon_de_commande="150222", month_key="2026-05", sequence=5,
            invoice_number="Invoice 20260505", po_number="DELAHK0105S",
        )
        blank = DocumentSequence.objects.create(
            bon_de_commande="150333", month_key="2026-05", sequence=6,
            invoice_number="Invoice 20260605", po_number="",
        )
        before = list(DocumentSequence.objects.order_by("pk").values())
        output = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("audit_document_sequences", stdout=output)
        result = json.loads(output.getvalue())
        self.assertEqual(result["inconsistent_count"], 2)
        rows = {row["document_sequence_id"]: row for row in result["records"]}
        self.assertEqual(rows[mismatch.pk]["expected_po"], "DELAHK0505S")
        self.assertTrue(rows[mismatch.pk]["invoice_ok"])
        self.assertFalse(rows[mismatch.pk]["po_ok"])
        self.assertEqual(rows[blank.pk]["stored_po"], "")
        self.assertEqual(list(DocumentSequence.objects.order_by("pk").values()), before)
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for query in queries
        ))

    def test_audit_handles_invalid_month_without_crashing(self):
        record = DocumentSequence.objects.create(
            bon_de_commande="bad-month", month_key="invalid", sequence=1,
        )
        from documents.services.document_numbering_service import audit_document_sequences
        result = list(audit_document_sequences())
        self.assertEqual(result[0]["document_sequence_id"], record.pk)
        self.assertIsNone(result[0]["expected_invoice"])
        self.assertFalse(result[0]["po_ok"])
