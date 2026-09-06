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
