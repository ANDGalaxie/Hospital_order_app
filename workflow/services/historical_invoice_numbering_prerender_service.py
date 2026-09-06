from __future__ import annotations

import hashlib
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings

from documents.models import DocumentSequence, GeneratedDocument
from documents.services.document_generation_service import (
    json_safe,
    load_json_config,
    render_invoice_html,
    sanitize_filename,
    save_json_file,
    write_invoice_html_and_pdf,
)
from documents.services.document_numbering_service import (
    build_expected_invoice_number,
)
from orders.models import Order
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_document_generation_service import (
    build_batch_hospital_invoice_data,
)


def build_batch_invoice_number(base_number: str, batch_number: Any) -> str:
    """Apply the project's existing ``-B<n>`` Invoice suffix rule."""
    number = int(batch_number or 1)
    return base_number if number <= 1 else f"{base_number}-B{number}"


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    return format(number.normalize(), "f")


def _pricing_basis(
    source_data: Dict[str, Any],
    invoice_data: Optional[Dict[str, Any]] = None,
    order_date: Any = None,
) -> Dict[str, Any]:
    basis = dict((source_data or {}).get("pricing_basis") or {})
    if not basis and invoice_data:
        order_date_text = order_date.isoformat() if order_date else ""
        basis = {
            "price_basis_type": "hospital_order_date",
            "hospital_order_date": order_date_text,
            "rows": [
                {
                    "product_code": row.get("product_code"),
                    "hospital_unit_price": row.get(
                        "hospital_unit_price",
                        row.get("unit_price_raw"),
                    ),
                    "hospital_order_date": (
                        row.get("hospital_order_date")
                        or order_date_text
                    ),
                    "line_total": row.get(
                        "line_total",
                        row.get("amount_raw"),
                    ),
                }
                for row in invoice_data.get("items") or []
            ],
        }
    rows = []
    for row in basis.get("rows") or []:
        rows.append(
            {
                "product_code": str(row.get("product_code") or ""),
                "hospital_unit_price": _decimal_text(
                    row.get("hospital_unit_price")
                ),
                "hospital_order_date": str(
                    row.get("hospital_order_date") or ""
                ),
                "line_total": _decimal_text(row.get("line_total")),
            }
        )
    basis["rows"] = sorted(
        rows,
        key=lambda row: (
            row["product_code"],
            row["hospital_unit_price"],
            row["line_total"],
        ),
    )
    return json_safe(basis)


def build_invoice_business_snapshot(
    source_data: Dict[str, Any],
    order_date: Any = None,
) -> Dict[str, Any]:
    """Return business data only; numbering and file paths are excluded."""
    source_data = source_data or {}
    invoice_data = source_data.get("invoice_data") or {}
    debug = invoice_data.get("debug") or {}
    invoice = invoice_data.get("invoice") or {}
    items = []
    for row in invoice_data.get("items") or []:
        items.append(
            {
                "product_code": str(row.get("product_code") or ""),
                "quantity": _decimal_text(
                    row.get("quantity_raw", row.get("quantity"))
                ),
                "hospital_unit_price": _decimal_text(
                    row.get(
                        "hospital_unit_price",
                        row.get("unit_price_raw"),
                    )
                ),
                "line_total": _decimal_text(
                    row.get("line_total", row.get("amount_raw"))
                ),
                "hospital_order_date": str(
                    row.get("hospital_order_date")
                    or (order_date.isoformat() if order_date else "")
                ),
            }
        )
    serials = []
    for row in invoice_data.get("serial_items") or []:
        serials.append(
            {
                "product_code": str(row.get("product_code") or ""),
                "quantity": _decimal_text(row.get("quantity")),
                "serial_number": str(row.get("serial_number") or ""),
                "expiration_date": str(row.get("expiration_date") or ""),
            }
        )
    totals = invoice_data.get("totals") or {}
    return json_safe(
        {
            "shipment_batch_id": (
                source_data.get("shipment_batch_id")
                or debug.get("shipment_batch_id")
            ),
            "items": sorted(
                items,
                key=lambda row: (
                    row["product_code"],
                    row["quantity"],
                    row["line_total"],
                ),
            ),
            "total": _decimal_text(
                totals.get("total_raw", totals.get("total"))
            ),
            "hospital_order_date": (
                _pricing_basis(
                    source_data,
                    invoice_data=invoice_data,
                    order_date=order_date,
                ).get("hospital_order_date")
                or next(
                    (
                        row["hospital_order_date"]
                        for row in items
                        if row["hospital_order_date"]
                    ),
                    "",
                )
            ),
            "serials": sorted(
                serials,
                key=lambda row: (
                    row["product_code"],
                    row["serial_number"],
                ),
            ),
            "invoice_address": list(
                (invoice_data.get("addresses") or {}).get(
                    "invoice_address"
                )
                or []
            ),
            "shipping_address": list(
                (invoice_data.get("addresses") or {}).get(
                    "shipping_address"
                )
                or []
            ),
            "pricing_basis": _pricing_basis(
                source_data,
                invoice_data=invoice_data,
                order_date=order_date,
            ),
            "invoice_date": str(invoice.get("invoice_date") or ""),
        }
    )


def _path_from_file_field(field: Any) -> Optional[Path]:
    name = str(field or "").strip()
    if not name:
        return None
    path = Path(name)
    if not path.is_absolute():
        path = Path(settings.MEDIA_ROOT) / path
    return path.resolve()


def file_fingerprint(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"path": "", "exists": False, "size": 0, "sha256": "", "mtime_ns": None}
    if not path.exists() or not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "size": 0,
            "sha256": "",
            "mtime_ns": None,
        }
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "sha256": digest.hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
    }


def production_file_fingerprints(plan_items: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {
        item["generated_document_id"]: {
            "pdf": file_fingerprint(item["current_pdf_resolved_path"]),
            "html": file_fingerprint(item["current_html_resolved_path"]),
        }
        for item in plan_items
    }


def build_historical_invoice_numbering_plan() -> Dict[str, Any]:
    """Build a read-only plan. This function never saves models or files."""
    orders = list(Order.objects.order_by("id"))
    sequences = list(DocumentSequence.objects.all().order_by("id"))
    sequences_by_key: Dict[Any, List[DocumentSequence]] = {}
    for sequence in sequences:
        sequences_by_key.setdefault(
            (sequence.month_key, sequence.bon_de_commande),
            [],
        ).append(sequence)

    workflows_by_document: Dict[int, List[DocumentWorkflowItem]] = {}
    workflows_by_batch: Dict[int, List[DocumentWorkflowItem]] = {}
    for workflow in DocumentWorkflowItem.objects.select_related(
        "shipment_batch",
        "order",
    ).order_by("id"):
        if workflow.invoice_document_id:
            workflows_by_document.setdefault(
                workflow.invoice_document_id,
                [],
            ).append(workflow)
        workflows_by_batch.setdefault(
            workflow.shipment_batch_id,
            [],
        ).append(workflow)

    invoice_documents = list(
        GeneratedDocument.objects.filter(
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE
        )
        .select_related("order", "shipment_batch")
        .order_by("order_id", "shipment_batch_id", "id")
    )
    documents_by_order: Dict[int, List[GeneratedDocument]] = {}
    for document in invoice_documents:
        documents_by_order.setdefault(document.order_id, []).append(document)

    order_items = []
    plan_items = []
    expected_full_numbers = []
    expected_base_numbers = []
    correct_orders = 0
    sequences_needing_update = 0

    for order in orders:
        blockers: List[str] = []
        expected = None
        sequence = None
        key = None
        if not order.order_date:
            blockers.append("missing_order_date")
        else:
            key = (
                order.order_date.strftime("%Y-%m"),
                order.bon_de_commande,
            )
            matching_sequences = sequences_by_key.get(key, [])
            if len(matching_sequences) > 1:
                blockers.append("multiple_document_sequences")
            sequence = matching_sequences[0] if matching_sequences else None
            expected = build_expected_invoice_number(order)
            expected_base_numbers.append(expected["invoice_number"])

        current_base = sequence.invoice_number if sequence else ""
        expected_base = expected["invoice_number"] if expected else ""
        current_sequence = sequence.sequence if sequence else None
        expected_sequence = expected["sequence"] if expected else None
        sequence_needs_update = bool(
            expected
            and (
                sequence is None
                or current_sequence != expected_sequence
                or current_base != expected_base
            )
        )
        if sequence_needs_update:
            sequences_needing_update += 1

        order_documents = documents_by_order.get(order.id, [])
        order_document_numbers_correct = True
        for document in order_documents:
            item_blockers = list(blockers)
            batch = document.shipment_batch
            if batch is None:
                item_blockers.append("missing_shipment_batch")
                expected_full = ""
                batch_number = None
            else:
                batch_number = batch.batch_number
                expected_full = (
                    build_batch_invoice_number(expected_base, batch_number)
                    if expected_base
                    else ""
                )
                expected_full_numbers.append(expected_full)
                if batch.order_id != order.id:
                    item_blockers.append("shipment_batch_order_mismatch")

            workflow_candidates = workflows_by_document.get(document.id) or (
                workflows_by_batch.get(document.shipment_batch_id, [])
                if document.shipment_batch_id
                else []
            )
            if len(workflow_candidates) != 1:
                item_blockers.append(
                    "missing_workflow_item"
                    if not workflow_candidates
                    else "multiple_workflow_items"
                )
                workflow = None
            else:
                workflow = workflow_candidates[0]
                if workflow.order_id != order.id:
                    item_blockers.append("workflow_order_mismatch")

            needs_update = bool(
                expected_full
                and document.document_number != expected_full
            )
            order_document_numbers_correct &= not needs_update
            plan_items.append(
                {
                    "order_id": order.id,
                    "bon_de_commande": order.bon_de_commande,
                    "order_date": order.order_date,
                    "current_sequence": current_sequence,
                    "expected_sequence": expected_sequence,
                    "current_base_number": current_base,
                    "expected_base_number": expected_base,
                    "shipment_batch_id": document.shipment_batch_id,
                    "batch_number": batch_number,
                    "workflow_item_id": workflow.id if workflow else None,
                    "generated_document_id": document.id,
                    "current_document_number": document.document_number,
                    "expected_document_number": expected_full,
                    "current_pdf_path": str(document.pdf_file or ""),
                    "current_html_path": str(document.html_file or ""),
                    "current_pdf_resolved_path": _path_from_file_field(
                        document.pdf_file
                    ),
                    "current_html_resolved_path": _path_from_file_field(
                        document.html_file
                    ),
                    "needs_update": needs_update,
                    "blockers": sorted(set(item_blockers)),
                    "business_snapshot": build_invoice_business_snapshot(
                        document.source_data or {},
                        order_date=order.order_date,
                    ),
                }
            )

        order_correct = bool(
            expected
            and sequence
            and not blockers
            and current_base == expected_base
            and order_document_numbers_correct
        )
        correct_orders += int(order_correct)
        order_items.append(
            {
                "order_id": order.id,
                "bon_de_commande": order.bon_de_commande,
                "order_date": order.order_date,
                "current_sequence": current_sequence,
                "expected_sequence": expected_sequence,
                "current_base_number": current_base,
                "expected_base_number": expected_base,
                "sequence_needs_update": sequence_needs_update,
                "correct": order_correct,
                "blockers": sorted(set(blockers)),
            }
        )

    duplicate_base = sorted(
        number
        for number, count in Counter(expected_base_numbers).items()
        if count > 1
    )
    duplicate_full = sorted(
        number
        for number, count in Counter(expected_full_numbers).items()
        if count > 1
    )
    if duplicate_full:
        for item in plan_items:
            if item["expected_document_number"] in duplicate_full:
                item["blockers"] = sorted(
                    set(item["blockers"] + ["duplicate_expected_document_number"])
                )

    blocker_count = sum(bool(item["blockers"]) for item in plan_items)
    affected = [item for item in plan_items if item["needs_update"]]
    return {
        "orders": order_items,
        "invoice_items": plan_items,
        "affected_invoice_items": affected,
        "summary": {
            "order_count": len(orders),
            "correct_order_count": correct_orders,
            "incorrect_order_count": len(orders) - correct_orders,
            "document_sequence_update_count": sequences_needing_update,
            "hospital_invoice_count": len(plan_items),
            "affected_hospital_invoice_count": len(affected),
            "expected_base_numbers_unique": not duplicate_base,
            "expected_document_numbers_unique": not duplicate_full,
            "duplicate_expected_base_numbers": duplicate_base,
            "duplicate_expected_document_numbers": duplicate_full,
            "blocker_count": blocker_count,
            "validate_render_allowed": blocker_count == 0 and not duplicate_full,
            "production_apply_status": "BLOCKED",
        },
    }


def _require_child_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"Temporary output escapes temporary root: {resolved_candidate}"
        ) from exc
    return resolved_candidate


def prerender_historical_invoice(
    plan_item: Dict[str, Any],
    temporary_root: Path,
) -> Dict[str, Any]:
    """Render one planned Invoice without saving any database model."""
    if plan_item.get("blockers"):
        raise ValueError(
            "Invoice prerender is blocked: "
            + ", ".join(plan_item["blockers"])
        )
    temporary_root = Path(temporary_root).resolve()
    if not temporary_root.exists() or not temporary_root.is_dir():
        raise ValueError("temporary_root must be an existing directory.")

    document = (
        GeneratedDocument.objects.select_related(
            "shipment_batch__order"
        )
        .get(id=plan_item["generated_document_id"])
    )
    if (
        document.document_type
        != GeneratedDocument.DocumentType.HOSPITAL_INVOICE
    ):
        raise ValueError("Only Hospital Invoice documents may be prerendered.")
    batch = document.shipment_batch
    if batch is None:
        raise ValueError("Hospital Invoice has no ShipmentBatch.")

    output_dir = _require_child_path(
        temporary_root,
        temporary_root
        / f"invoice_{document.id}_batch_{batch.id}",
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    base_name = sanitize_filename(plan_item["expected_document_number"])
    html_path = _require_child_path(
        temporary_root,
        output_dir / f"{base_name}.html",
    )
    pdf_path = _require_child_path(
        temporary_root,
        output_dir / f"{base_name}.pdf",
    )
    data_path = _require_child_path(
        temporary_root,
        output_dir / f"{base_name}_data.json",
    )

    numbers = {
        "invoice_number": plan_item["expected_document_number"],
        "base_invoice_number": plan_item["expected_base_number"],
        "batch_number": plan_item["batch_number"],
        "batch_numbering_warnings": [],
        "sequence": plan_item["expected_sequence"],
        "document_date": (
            plan_item["order_date"].isoformat()
            if plan_item["order_date"]
            else ""
        ),
    }
    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )
    invoice_data = build_batch_hospital_invoice_data(
        batch=batch,
        company_info=company_info,
        numbers=numbers,
    )
    source_data = {
        "workflow_item_id": plan_item["workflow_item_id"],
        "shipment_batch_id": batch.id,
        "numbers": numbers,
        "pricing_basis": {
            "price_basis_type": "hospital_order_date",
            "hospital_order_date": batch.order.order_date.isoformat(),
            "rows": [
                {
                    "product_code": row["product_code"],
                    "hospital_unit_price": str(row["unit_price_raw"]),
                    "hospital_order_date": batch.order.order_date.isoformat(),
                    "line_total": str(row["amount_raw"]),
                }
                for row in invoice_data["items"]
            ],
        },
        "invoice_data": invoice_data,
    }
    html_content = render_invoice_html(
        invoice_data=invoice_data,
        template_dir=Path(settings.BASE_DIR) / "templates",
        template_name="hospital_invoice.html",
    )
    write_invoice_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=Path(settings.BASE_DIR),
    )
    save_json_file(source_data, data_path)

    html_exists = html_path.is_file() and html_path.stat().st_size > 0
    html_number_valid = (
        html_exists
        and plan_item["expected_document_number"]
        in html_path.read_text(encoding="utf-8")
    )
    pdf_exists = pdf_path.is_file() and pdf_path.stat().st_size > 0
    pdf_structure_valid = False
    pdf_page_count = 0
    pdf_error = ""
    if pdf_exists:
        try:
            import pdfplumber

            with pdfplumber.open(str(pdf_path)) as pdf:
                pdf_page_count = len(pdf.pages)
            pdf_structure_valid = pdf_page_count > 0
        except Exception as exc:
            pdf_error = f"{type(exc).__name__}: {exc}"

    after_snapshot = build_invoice_business_snapshot(
        source_data,
        order_date=batch.order.order_date,
    )
    before_snapshot = plan_item["business_snapshot"]
    snapshot_matches = before_snapshot == after_snapshot
    validations = {
        "html_exists_nonempty": html_exists,
        "html_number_valid": html_number_valid,
        "pdf_exists_nonempty": pdf_exists,
        "pdf_structure_valid": pdf_structure_valid,
        "pdf_page_count": pdf_page_count,
        "temporary_paths_valid": all(
            _require_child_path(temporary_root, path)
            for path in (html_path, pdf_path, data_path)
        ),
        "business_snapshot_matches": snapshot_matches,
    }
    warnings = list(invoice_data.get("warnings") or [])
    if pdf_error:
        warnings.append(pdf_error)
    if not snapshot_matches:
        warnings.append("Business snapshot differs from the existing Invoice.")

    return {
        "html_path": html_path,
        "pdf_path": pdf_path,
        "data_path": data_path,
        "source_data": json_safe(source_data),
        "business_snapshot": after_snapshot,
        "html_size": html_path.stat().st_size if html_path.exists() else 0,
        "pdf_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
        "html_sha256": file_fingerprint(html_path)["sha256"],
        "pdf_sha256": file_fingerprint(pdf_path)["sha256"],
        "validations": validations,
        "warnings": warnings,
        "success": all(
            value
            for key, value in validations.items()
            if key != "pdf_page_count"
        ),
    }
