from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_numbering_service import (
    build_invoice_number,
    build_po_number,
)
from orders.models import Order
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_validation_service import (
    validate_document_workflow_item,
)


EXCEPTION_BON = "152376"
EXCEPTION_SEQUENCE = 2
EXCEPTION_YEAR = 2026
EXCEPTION_MONTH = 7


class Command(BaseCommand):
    help = (
        "Generate the strictly scoped historical numbering exception for "
        "Hospital Order 152376."
    )

    def add_arguments(self, parser):
        parser.add_argument("--bon", required=True)
        parser.add_argument("--sequence", required=True, type=int)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        bon = str(options["bon"] or "").strip()
        sequence_value = int(options["sequence"])

        if bon != EXCEPTION_BON or sequence_value != EXCEPTION_SEQUENCE:
            raise CommandError(
                "This one-time command only permits --bon 152376 --sequence 2."
            )

        try:
            order = Order.objects.select_related("created_by").get(
                bon_de_commande=bon
            )
        except Order.DoesNotExist as exc:
            raise CommandError(f"Order {bon} does not exist.") from exc

        blockers = []
        if not order.order_date:
            blockers.append("Order.order_date is missing.")
        elif (
            order.order_date.year != EXCEPTION_YEAR
            or order.order_date.month != EXCEPTION_MONTH
        ):
            blockers.append(
                "Order.order_date must be in 2026-07 for this exception."
            )

        if not order.created_by_id:
            blockers.append("Order.created_by is required as generated_by.")

        batches = list(
            ShipmentBatch.objects.filter(order=order).order_by("id")
        )
        if len(batches) != 1:
            blockers.append(
                f"Expected exactly one ShipmentBatch; found {len(batches)}."
            )
            batch = None
        else:
            batch = batches[0]

        workflow_items = list(
            DocumentWorkflowItem.objects.filter(order=order).order_by("id")
        )
        if len(workflow_items) != 1:
            blockers.append(
                f"Expected exactly one WorkflowItem; found {len(workflow_items)}."
            )
            item = None
        else:
            item = workflow_items[0]
            if batch and item.shipment_batch_id != batch.id:
                blockers.append("WorkflowItem does not belong to the target batch.")

        if order.order_date:
            month_key = order.order_date.strftime("%Y-%m")
            invoice_number = build_invoice_number(
                order.order_date,
                sequence_value,
            )
            po_number = build_po_number(
                order.order_date,
                sequence_value,
            )
        else:
            month_key = ""
            invoice_number = ""
            po_number = ""

        if invoice_number and invoice_number != "Invoice 20260207":
            blockers.append(
                f"Unexpected Invoice number for exception: {invoice_number}."
            )
        if po_number and po_number != build_po_number(order.order_date, 2):
            blockers.append(f"Unexpected PO number for exception: {po_number}.")

        target_sequence = None
        if month_key:
            target_sequence = DocumentSequence.objects.filter(
                month_key=month_key,
                bon_de_commande=bon,
            ).first()
            occupied = DocumentSequence.objects.filter(
                month_key=month_key,
                sequence=sequence_value,
            ).exclude(bon_de_commande=bon).first()
            if occupied:
                blockers.append(
                    "Sequence 2 is already occupied by "
                    f"{occupied.bon_de_commande} ({occupied.invoice_number})."
                )

        if target_sequence:
            if target_sequence.sequence != sequence_value:
                blockers.append(
                    "Existing target DocumentSequence has a different sequence."
                )
            if target_sequence.invoice_number not in ("", invoice_number):
                blockers.append(
                    "Existing target DocumentSequence has a different Invoice number."
                )
            if target_sequence.po_number not in ("", po_number):
                blockers.append(
                    "Existing target DocumentSequence has a different PO number."
                )

        formal_documents = list(
            GeneratedDocument.objects.filter(
                order=order,
                document_type__in=[
                    GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
                    GeneratedDocument.DocumentType.FACTORY_PO,
                ],
            ).order_by("id")
        )
        if formal_documents:
            blockers.append(
                "Target Order already has formal Invoice/PO documents: "
                + ", ".join(
                    f"#{document.id} {document.document_type} "
                    f"{document.document_number}"
                    for document in formal_documents
                )
            )

        if item and (item.invoice_document_id or item.po_document_id):
            blockers.append("WorkflowItem already references a generated Invoice/PO.")

        for document_type, number in (
            (GeneratedDocument.DocumentType.HOSPITAL_INVOICE, invoice_number),
            (GeneratedDocument.DocumentType.FACTORY_PO, po_number),
        ):
            conflict = GeneratedDocument.objects.filter(
                document_type=document_type,
                document_number=number,
            ).exclude(order=order).first()
            if conflict:
                blockers.append(
                    f"Document number {number} is already used by "
                    f"GeneratedDocument #{conflict.id}."
                )

        validation = None
        if item:
            validation = validate_document_workflow_item(item=item, save=False)
            if not validation.get("can_generate_documents"):
                blockers.append(
                    "Workflow validation failed: "
                    + "; ".join(
                        str(value)
                        for value in (
                            (validation.get("errors") or [])
                            + (validation.get("warnings") or [])
                        )
                    )
                )

        self.stdout.write(
            "PLAN "
            f"order_id={order.id} "
            f"batch_id={getattr(batch, 'id', None)} "
            f"workflow_item_id={getattr(item, 'id', None)} "
            f"month_key={month_key} "
            f"sequence={sequence_value} "
            f"invoice={invoice_number} "
            f"po={po_number} "
            f"existing_sequence_id={getattr(target_sequence, 'id', None)} "
            f"formal_documents={len(formal_documents)} "
            f"validation_ready={bool(validation and validation.get('can_generate_documents'))} "
            f"blockers={len(blockers)}"
        )

        if blockers:
            raise CommandError(" | ".join(blockers))
        if not options["confirm"]:
            raise CommandError(
                "No changes made. Re-run with --confirm to execute this exception."
            )

        from workflow.services import workflow_document_generation_service

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order.id)
            batch = ShipmentBatch.objects.select_for_update().get(id=batch.id)
            item = DocumentWorkflowItem.objects.select_for_update().get(id=item.id)

            occupied = DocumentSequence.objects.select_for_update().filter(
                month_key=month_key,
                sequence=sequence_value,
            ).exclude(bon_de_commande=bon).first()
            if occupied:
                raise CommandError(
                    f"Sequence 2 became occupied by {occupied.bon_de_commande}."
                )

            sequence_record = DocumentSequence.objects.select_for_update().filter(
                month_key=month_key,
                bon_de_commande=bon,
            ).first()
            created = sequence_record is None
            if created:
                sequence_record = DocumentSequence.objects.create(
                    month_key=month_key,
                    bon_de_commande=bon,
                    sequence=sequence_value,
                    invoice_number=invoice_number,
                    po_number=po_number,
                )
            else:
                sequence_record.sequence = sequence_value
                sequence_record.invoice_number = invoice_number
                sequence_record.po_number = po_number
                sequence_record.save(
                    update_fields=[
                        "sequence",
                        "invoice_number",
                        "po_number",
                        "updated_at",
                    ]
                )

            assigned_numbers = {
                "year": order.order_date.year,
                "month": order.order_date.month,
                "sequence": sequence_value,
                "sort_key": None,
                "invoice_number": invoice_number,
                "sequence_id": sequence_record.id,
                "created": created,
            }

            with patch.object(
                workflow_document_generation_service,
                "get_or_create_expected_invoice_numbers",
                return_value=assigned_numbers,
            ):
                result = (
                    workflow_document_generation_service
                    .generate_documents_for_workflow_item(
                        item=item,
                        generated_by=order.created_by,
                    )
                )

            invoice_result = result["invoice"]
            po_result = result["factory_po"]
            if invoice_result["document_number"] != invoice_number:
                raise CommandError(
                    "Generated Invoice number does not match the exception."
                )
            if po_result["document_number"] != po_number:
                raise CommandError(
                    "Generated PO number does not match the exception."
                )

            for label, document_result in (
                ("Invoice", invoice_result),
                ("Factory PO", po_result),
            ):
                pdf_path = Path(document_result["pdf_path"])
                if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                    raise CommandError(
                        f"{label} PDF was not created: {pdf_path}"
                    )

        self.stdout.write(
            self.style.SUCCESS(
                "RESULT "
                f"order_id={order.id} "
                f"batch_id={batch.id} "
                f"workflow_item_id={item.id} "
                f"sequence_id={sequence_record.id} "
                f"invoice_document_id={invoice_result['generated_document_id']} "
                f"invoice={invoice_result['document_number']} "
                f"invoice_pdf={invoice_result['pdf_path']} "
                f"po_document_id={po_result['generated_document_id']} "
                f"po={po_result['document_number']} "
                f"po_pdf={po_result['pdf_path']}"
            )
        )
