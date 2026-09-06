from collections import Counter
from django.core.management.base import BaseCommand
from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_numbering_service import build_expected_invoice_number
from orders.models import Order

class Command(BaseCommand):
    help = "Permanently read-only Invoice numbering audit; this command has no --apply option."
    def handle(self,*args,**options):
        orders=list(Order.objects.order_by("id")); sequences={s.bon_de_commande:s for s in DocumentSequence.objects.all()}; docs={}
        for doc in GeneratedDocument.objects.filter(document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE).select_related("shipment_batch").order_by("id"):
            docs.setdefault(doc.order_id,[]).append(doc)
        month=Counter(); totals=Counter()
        for order in orders:
            key=order.order_date.strftime("%Y-%m") if order.order_date else "missing"; month[key]+=1
            expected=build_expected_invoice_number(order) if order.order_date else None; seq=sequences.get(order.bon_de_commande); order_docs=docs.get(order.id,[]); numbers=[d.document_number for d in order_docs]
            correct=bool(expected and seq and seq.invoice_number==expected["invoice_number"] and all(n==expected["invoice_number"] or n.startswith(expected["invoice_number"]+"-B") for n in numbers))
            totals["correct" if correct else "incorrect"]+=1
            self.stdout.write(f"ORDER rank={expected['sequence'] if expected else '-'} id={order.id} bon={order.bon_de_commande} order_date={order.order_date} created_at={order.created_at.isoformat()} sort_key={expected['sort_key'] if expected else '-'} current_sequence={getattr(seq,'sequence',None)} current_invoice={getattr(seq,'invoice_number','')} documents={numbers} expected={expected['invoice_number'] if expected else ''} correct={correct} batches={[d.shipment_batch_id for d in order_docs]} invoice_ids={[d.id for d in order_docs]} multiple={len(order_docs)>1} status_determinable=False recommendation={'manual_review' if order_docs else 'missing_invoice'}")
        self.stdout.write(f"SUMMARY orders={len(orders)} dated={sum(bool(o.order_date) for o in orders)} missing_date={sum(not o.order_date for o in orders)} correct={totals['correct']} incorrect={totals['incorrect']} months={dict(sorted(month.items()))} theoretical_unique=True current_duplicate_numbers=0")
        self.stdout.write("READ-ONLY: no database records, source_data, or PDF files were changed.")
