import logging
import os
import tempfile
import uuid
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from documents.models import GeneratedDocument
from documents.services.document_generation_service import render_po_html, write_po_html_and_pdf
from shipments.models import ShipmentBatch

from .batch_facts_service import fingerprint
from .factory_payload_service import (
    factory_html_fingerprint, frozen_row_quantity, non_price_payload, non_price_html,
    reprice_factory_payload, reprice_frozen_factory_html,
    source_factory_po, validate_frozen_payload,
)
from workflow.services.workflow_document_generation_service import get_batch_shipping_date
from .price_service import CommercialPOError, MONEY_QUANT, resolve_commercial_price


logger = logging.getLogger(__name__)
TYPE = GeneratedDocument.DocumentType.COMMERCIAL_PO


def commercial_po_number(batch):
    # The order PK avoids collisions between distinct non-numeric BON strings.
    # Numeric BONs keep the short, familiar CPO-147891-B1 format.
    bon = str(batch.order.bon_de_commande)
    identity = bon if bon.isascii() and bon.isdigit() else f"O{batch.order_id}"
    return f"CPO-{identity}-B{batch.batch_number}"


def build_commercial_snapshot(batch, *, document_number=None):
    factory_document = source_factory_po(batch)
    original = factory_document.source_data["po_data"]
    shipping_date = get_batch_shipping_date(batch)
    if bool(batch.factory_confirmation_id) == bool(batch.inventory_allocation_id):
        raise CommercialPOError("Exactly one supported batch source is required.")
    if batch.factory_confirmation_id:
        source = batch.factory_confirmation
        if (source.order_id != batch.order_id or source.shipping_date != shipping_date
                or batch.source_type != "factory_confirmation"):
            raise CommercialPOError("Batch date and factory shipping date are missing or conflict.")
        date_source = "shipment_batch.batch_date / factory_confirmation.shipping_date"
    else:
        source = batch.inventory_allocation
        if source.order_id != batch.order_id or batch.source_type != "inventory_allocation":
            raise CommercialPOError("Inventory source does not belong to this batch order.")
        date_source = "shipment_batch.batch_date"
    policy, unit_price = resolve_commercial_price(shipping_date)
    po_data = reprice_factory_payload(original, unit_price)
    # Statistics projection only. Rendering always uses the unchanged row list
    # in po_data, including separate rows for the same product's old discounts.
    projected = {}
    for row in original["items"]:
        code = row["product_code"]
        if code not in projected:
            projected[code] = {"batch_item_id": None, "order_item_id": None, "product_id": None,
                               "product_code": code, "description": row["description"], "quantity": Decimal(0)}
        projected[code]["quantity"] += frozen_row_quantity(row)
    facts = {
        "order_id": batch.order_id, "bon_de_commande": str(batch.order.bon_de_commande),
        "hospital_id": batch.order.hospital_id,
        "hospital_name": batch.order.hospital_name or (batch.order.hospital.name if batch.order.hospital else ""),
        "shipment_batch_id": batch.pk, "batch_number": batch.batch_number,
        "shipping_date": shipping_date.isoformat(), "shipping_date_source": date_source,
        "source_type": batch.source_type, "source_id": source.pk,
        "shipping_address": deepcopy(original["shipping_address"]), "serials": [],
        "items": [{**row, "quantity": str(row["quantity"].quantize(Decimal("1")))} for row in projected.values()],
    }
    items = []
    total_units = Decimal("0")
    total_amount = Decimal("0.00")
    for fact in facts["items"]:
        quantity = Decimal(fact["quantity"])
        amount = (quantity * unit_price).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        items.append({**fact, "unit_price": str(unit_price), "line_amount": str(amount)})
        total_units += quantity
        total_amount += amount
    return {
        **facts, "document_type": TYPE, "schema_version": 1,
        "document_number": document_number or commercial_po_number(batch), "issuer": "Acoeur",
        "price_policy_id": policy.pk,
        "rule_start_date": policy.start_date.isoformat() if policy.start_date else None,
        "rule_end_date": policy.end_date.isoformat() if policy.end_date else None,
        "currency": "EUR", "unit_price": str(unit_price),
        "items": items, "total_units": str(total_units), "total_amount": str(total_amount),
        "facts_fingerprint": fingerprint(facts),
        "po_data": po_data,
        "source_factory_po_document_id": factory_document.pk,
        "source_factory_po_document_number": factory_document.document_number,
        "source_factory_po_fingerprint": fingerprint(original),
        "source_factory_po_html_fingerprint": factory_html_fingerprint(factory_document),
        "source_factory_po_non_price_fingerprint": fingerprint(non_price_payload(original)),
        "commercial_payload_fingerprint": fingerprint(po_data),
        "commercial_pricing_basis": {"reference_date": shipping_date.isoformat(), "reference_date_source": date_source,
                                     "price_policy_id": policy.pk, "unit_price": str(unit_price), "currency": "EUR"},
        "rendering": {"template": "templates/factory_purchase_order.html", "rendered_at": timezone.now().isoformat()},
    }


def build_commercial_po_data(snapshot):
    """Return the frozen, price-only patched clone of the same batch's PO."""
    validate_frozen_payload(snapshot)
    if "po_data" not in snapshot:
        raise CommercialPOError("Commercial render payload requires the same batch's frozen Factory PO.")
    return deepcopy(snapshot["po_data"])


def render_commercial_files(snapshot, directory):
    po_data = build_commercial_po_data(snapshot)
    html = render_po_html(po_data=po_data, template_path=Path(settings.BASE_DIR) / "templates/factory_purchase_order.html")
    factory_document = GeneratedDocument.objects.get(
        pk=snapshot["source_factory_po_document_id"], shipment_batch_id=snapshot["shipment_batch_id"],
        document_type=GeneratedDocument.DocumentType.FACTORY_PO,
    )
    if not factory_document.html_file or not factory_document.html_file.storage.exists(factory_document.html_file.name):
        raise CommercialPOError("Source Factory PO HTML is unavailable; visual parity cannot be verified.")
    with factory_document.html_file.open("rb") as reference:
        reference_html = reference.read().decode("utf-8")
    if non_price_html(reference_html) != non_price_html(html):
        html = reprice_frozen_factory_html(reference_html, factory_document.source_data["po_data"], po_data)
        snapshot["rendering"]["template"] = "source_factory_po.html_file"
        snapshot["rendering"]["mode"] = "frozen_factory_html"
    else:
        snapshot["rendering"]["mode"] = "current_factory_template"
    html_path, pdf_path = directory / "document.html", directory / "document.pdf"
    write_po_html_and_pdf(html_content=html, html_path=html_path, pdf_path=pdf_path, project_root=Path(settings.BASE_DIR))
    if not html_path.is_file() or not html_path.read_text(encoding="utf-8").strip():
        raise CommercialPOError("Commercial HTML rendering did not produce a file.")
    if not pdf_path.is_file() or pdf_path.stat().st_size < 100:
        raise CommercialPOError("Commercial PDF rendering did not produce a valid file.")
    with pdf_path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise CommercialPOError("Commercial PDF signature is invalid.")
    import fitz
    try:
        with fitz.open(pdf_path) as pdf:
            text = "\n".join(page.get_text() for page in pdf)
            if not pdf.page_count or po_data["po"]["po_number"] not in text:
                raise CommercialPOError("Commercial PDF content validation failed.")
            prohibited = ("commercial presentation only", "commercial purchase order", "not a factory payment",
                          "not applicable to this commercial display", "for hospital commercial presentation only")
            if any(label in text.casefold() for label in prohibited):
                raise CommercialPOError("Commercial PDF contains an unexpected display label.")
    except (RuntimeError, fitz.FileDataError) as exc:
        raise CommercialPOError("Commercial PDF is unreadable.") from exc


def _existing(batch_id):
    return GeneratedDocument.objects.filter(shipment_batch_id=batch_id, document_type=TYPE).first()


def _require_existing_files(document):
    if not document.pdf_file or not document.html_file:
        raise CommercialPOError("Existing commercial document has missing files; manual investigation required.")
    if not document.pdf_file.storage.exists(document.pdf_file.name) or not document.html_file.storage.exists(document.html_file.name):
        raise CommercialPOError("Existing commercial document files are unavailable; snapshot is not regenerated.")
    return document


def ensure_commercial_po_for_batch(batch_id, generated_by, *, regenerate_existing=False):
    """Render outside transactions, then publish under a short batch lock.

    A random attempt directory makes concurrent renderers and filesystem rollback
    independent. Only this call's provisional files may be cleaned on failure.
    Existing documents are reused unless regeneration is explicitly requested.
    Regeneration clones the same batch's frozen Factory PO and resolves only commercial prices,
    and updates the same record only after new files have been validated.
    Old files and document identity are always retained.
    """
    if connection.in_atomic_block:
        raise CommercialPOError("Schedule commercial generation after the original transaction commits.")
    existing = _existing(batch_id)
    if existing and not regenerate_existing:
        return _require_existing_files(existing)
    batch = ShipmentBatch.objects.select_related("order__hospital", "factory_confirmation", "inventory_allocation").get(pk=batch_id)
    replacing = existing if regenerate_existing else None
    snapshot = build_commercial_snapshot(batch, document_number=replacing.document_number if replacing else None)
    root = Path(settings.MEDIA_ROOT).resolve()
    parent = root / "commercial_purchase_orders" / f"order_{batch.order_id}" / f"batch_{batch.pk}"
    parent.mkdir(parents=True, exist_ok=True)
    published = None
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=parent) as pending:
            pending = Path(pending)
            render_commercial_files(snapshot, pending)
            with transaction.atomic():
                # Serializes publication, not rendering; partial unique index is the backstop.
                locked_batch = ShipmentBatch.objects.select_for_update().get(pk=batch_id)
                current = _existing(batch_id)
                if replacing:
                    if current is None or current.pk != replacing.pk:
                        raise CommercialPOError("Existing commercial document changed during rendering; retry required.")
                    current = GeneratedDocument.objects.select_for_update().get(pk=current.pk, document_type=TYPE)
                    revision_fields = ("order_id", "shipment_batch_id", "document_number", "source_data",
                                       "pdf_file", "html_file", "generated_by_id", "generated_at")
                    if (current.order_id != locked_batch.order_id
                            or any(getattr(current, field) != getattr(replacing, field) for field in revision_fields)):
                        raise CommercialPOError("Existing commercial document changed during rendering; retry required.")
                elif current:
                    return _require_existing_files(current)
                fresh = build_commercial_snapshot(locked_batch, document_number=snapshot["document_number"])
                if any(fresh[key] != snapshot[key] for key in (
                    "source_factory_po_document_id", "source_factory_po_document_number", "source_factory_po_fingerprint", "source_factory_po_html_fingerprint",
                    "facts_fingerprint", "commercial_payload_fingerprint", "price_policy_id", "rule_start_date", "rule_end_date",
                )):
                    raise CommercialPOError("Batch facts changed during rendering; retry required.")
                published = parent / f"generation_{uuid.uuid4().hex}"
                os.rename(pending, published)
                files = {"pdf_file": str((published / "document.pdf").relative_to(root)),
                         "html_file": str((published / "document.html").relative_to(root))}
                if replacing:
                    GeneratedDocument.objects.filter(pk=current.pk, document_type=TYPE).update(
                        **files, source_data=snapshot, generated_by=generated_by, generated_at=timezone.now(),
                    )
                    current.refresh_from_db()
                    document = current
                else:
                    document = GeneratedDocument.objects.create(
                        order_id=batch.order_id, shipment_batch_id=batch.pk, document_type=TYPE,
                        document_number=snapshot["document_number"], source_data=snapshot,
                        generated_by=generated_by, **files,
                    )
            committed = True
            return document
    finally:
        # A commit error can have an uncertain outcome (e.g. lost commit ACK).
        # Never delete published files: the database may already reference them.
        # TemporaryDirectory cleans only the unrenamed temporary rendering files.
        if published is not None and not committed and published.is_dir():
            logger.warning("Retained commercial publication files for reconciliation: batch_id=%s order_id=%s", batch.pk, batch.order_id)


def schedule_commercial_po(batch_id, order_id, generated_by):
    """on_commit is synchronous here; it is not a background queue."""
    def extend():
        try:
            ensure_commercial_po_for_batch(batch_id, generated_by)
        except Exception:
            logger.exception("Commercial PO extension failed: batch_id=%s order_id=%s", batch_id, order_id)
    transaction.on_commit(extend)
