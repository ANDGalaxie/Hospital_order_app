from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

from django.conf import settings
from django.db import transaction

from documents.models import GeneratedDocument
from documents.services.document_numbering_service import (
    get_or_create_document_numbers,
    parse_document_date,
)
from documents.services.document_generation_service import (
    DEFAULT_FACTORY_UNIT_PRICE,
    EXPECTED_ARRIVAL_DAYS,
    EXPIRATION_THRESHOLD_DAYS,
    build_factory_info_from_model,
    format_date_display,
    format_eur,
    format_quantity,
    format_serial_quantity,
    format_po_quantity,
    format_po_unit_price,
    format_po_discount,
    format_po_eur,
    get_hospital_order_date,
    get_invoice_address_lines,
    get_order_document_workspace,
    get_po_shipping_address_lines,
    get_shipping_address_lines,
    json_safe,
    load_json_config,
    media_relative_path,
    prepare_company_info,
    prepare_factory_info,
    render_invoice_html,
    render_po_html,
    sanitize_filename,
    save_json_file,
    should_apply_expiration_discount,
    write_invoice_html_and_pdf,
    write_po_html_and_pdf,
)
from factory_confirmations.models import SerialItem
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem

try:
    from backorders.models import InventoryItem
except Exception:
    InventoryItem = None


def format_date_fr_from_date(value):
    """
    Invoice 模板使用 dd/mm/yyyy 风格。
    """
    d = parse_document_date(value)
    return d.strftime("%d/%m/%Y")


def get_quantity_from_batch_item(batch_item) -> int:
    return int(batch_item.shipped_quantity or 0)


def get_batch_order_item(batch: ShipmentBatch, product_code: str):
    return batch.order.items.filter(product_code=product_code).first()


def get_batch_shipping_date(batch: ShipmentBatch):
    """
    当前 batch 的实际发货日期。

    Invoice date = batch_date
    Discount reference date = batch_date
    Expected arrival = batch_date + 3 days
    """
    if batch.batch_date:
        return batch.batch_date

    raise ValueError(
        f"ShipmentBatch {batch.id}: batch_date is missing."
    )


def get_batch_po_order_date(batch: ShipmentBatch):
    """
    PO Order Date / document sequence date = 医院订单 Date de commande。

    如果医院订单日期缺失，fallback 到 batch shipping date。
    """
    hospital_order_date, source = get_hospital_order_date(batch.order)

    if hospital_order_date:
        return hospital_order_date, source, []

    shipping_date = get_batch_shipping_date(batch)

    return (
        shipping_date,
        "fallback_batch_date",
        [
            f"Order {batch.order.bon_de_commande}: hospital order date is missing. "
            f"Use batch_date {shipping_date.isoformat()} for PO order date and sequence."
        ],
    )


def get_batch_document_numbers(batch: ShipmentBatch) -> Dict[str, Any]:
    """
    Batch-level 编号规则：

    Batch 1:
      Invoice 20260106
      DELAHK0106S

    Batch 2:
      Invoice 20260106-B2
      DELAHK0106S-B2

    Batch 3:
      Invoice 20260106-B3
      DELAHK0106S-B3
    """
    po_order_date, date_source, warnings = get_batch_po_order_date(batch)

    numbers = get_or_create_document_numbers(
        bon_de_commande=batch.order.bon_de_commande,
        document_date=po_order_date,
    )

    numbers = dict(numbers)
    numbers["base_invoice_number"] = numbers["invoice_number"]
    numbers["base_po_number"] = numbers["po_number"]
    numbers["batch_number"] = batch.batch_number
    numbers["batch_date_source"] = date_source
    numbers["batch_numbering_warnings"] = warnings

    batch_number = int(batch.batch_number or 1)

    if batch_number > 1:
        numbers["invoice_number"] = (
            f"{numbers['base_invoice_number']}-B{batch_number}"
        )
        numbers["po_number"] = (
            f"{numbers['base_po_number']}-B{batch_number}"
        )

    return numbers


def get_batch_serial_rows(batch: ShipmentBatch) -> List[Dict[str, Any]]:
    """
    当前 batch 的 serial 明细。

    FactoryConfirmation 来源：
      SerialItem

    InventoryAllocation 来源：
      InventoryItem
    """
    rows: List[Dict[str, Any]] = []

    if batch.factory_confirmation_id:
        serials = (
            SerialItem.objects
            .filter(
                order=batch.order,
                factory_confirmation=batch.factory_confirmation,
            )
            .order_by("product_code", "serial_number", "id")
        )

        for serial in serials:
            delivered_quantity = Decimal("1.00")

            raw_data = serial.raw_data or {}
            raw_qty = raw_data.get("delivered_quantity")

            if raw_qty is not None:
                try:
                    delivered_quantity = Decimal(str(raw_qty))
                except Exception:
                    delivered_quantity = Decimal("1.00")

            rows.append(
                {
                    "source": "factory_confirmation",
                    "source_id": serial.id,
                    "product": serial.product,
                    "product_code": str(serial.product_code or "").strip(),
                    "serial_number": str(serial.serial_number or "").strip(),
                    "expiration_date": serial.expiration_date,
                    "quantity": delivered_quantity,
                }
            )

    elif batch.inventory_allocation_id:
        if InventoryItem is None:
            raise ValueError("InventoryItem model is not available.")

        items = (
            InventoryItem.objects
            .filter(allocation=batch.inventory_allocation)
            .order_by("product_code", "serial_number", "id")
        )

        for item in items:
            rows.append(
                {
                    "source": "inventory_allocation",
                    "source_id": item.id,
                    "product": item.product,
                    "product_code": str(item.product_code or "").strip(),
                    "serial_number": str(item.serial_number or "").strip(),
                    "expiration_date": item.expiration_date,
                    "quantity": Decimal("1.00"),
                }
            )

    else:
        raise ValueError(
            f"ShipmentBatch {batch.id}: no factory_confirmation or inventory_allocation."
        )

    return rows


def build_batch_invoice_items(batch: ShipmentBatch) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Invoice 产品行只来自当前 ShipmentBatchItem。

    不能使用 OrderItem.confirmed_quantity，因为那是累计数量。
    """
    warnings: List[str] = []
    items: List[Dict[str, Any]] = []

    batch_items = (
        ShipmentBatchItem.objects
        .filter(batch=batch)
        .order_by("product_code", "id")
    )

    for batch_item in batch_items:
        product_code = str(batch_item.product_code or "").strip()
        quantity = get_quantity_from_batch_item(batch_item)

        if quantity <= 0:
            continue

        order_item = get_batch_order_item(batch, product_code)

        if order_item:
            description = (
                order_item.product.description
                if order_item.product and order_item.product.description
                else order_item.description
            )

            unit_price = Decimal(order_item.hospital_unit_price or 0)
        else:
            raise ValueError(
                f"ShipmentBatchItem {batch_item.id}: "
                f"product {product_code} "
                "does not exist in OrderItem."
            )

        if unit_price <= 0:
            raise ValueError(
                f"Product {product_code}: "
                "hospital_unit_price snapshot "
                "is missing or zero."
            )

        if not order_item.price_policy_id:
            raise ValueError(
                f"Product {product_code}: "
                "PricePolicy snapshot is missing."
            )

        amount = unit_price * Decimal(quantity)

        items.append(
            {
                "product_code": product_code,
                "description": description or "",
                "quantity_raw": float(quantity),
                "quantity": format_quantity(float(quantity)),
                "unit_price_raw": float(unit_price),
                "unit_price": format_eur(float(unit_price)),
                "amount_raw": float(amount),
                "amount": format_eur(float(amount)),
            }
        )

    if not items:
        warnings.append("No ShipmentBatchItem found for invoice.")

    return items, warnings


def build_batch_invoice_serial_items(batch: ShipmentBatch) -> List[Dict[str, Any]]:
    result = []

    for row in get_batch_serial_rows(batch):
        result.append(
            {
                "product_code": row["product_code"],
                "quantity": format_serial_quantity(float(row["quantity"])),
                "serial_number": row["serial_number"],
                "expiration_date": (
                    row["expiration_date"].isoformat()
                    if row["expiration_date"]
                    else ""
                ),
            }
        )

    return result


def build_batch_hospital_invoice_data(
    batch: ShipmentBatch,
    company_info: Dict[str, Any],
    numbers: Dict[str, Any],
) -> Dict[str, Any]:
    order = batch.order
    invoice_date = get_batch_shipping_date(batch)
    due_date = invoice_date + timedelta(
        days=int(company_info.get("payment_terms_days", 30))
    )

    items, warnings = build_batch_invoice_items(batch)
    serial_items = build_batch_invoice_serial_items(batch)

    untaxed_amount = sum(float(item.get("amount_raw") or 0) for item in items)
    total_units = sum(float(item.get("quantity_raw") or 0) for item in items)
    vat = 0.0
    total = untaxed_amount + vat

    invoice_data = {
        "invoice": {
            "invoice_number": numbers["invoice_number"],
            "invoice_date": format_date_fr_from_date(invoice_date),
            "due_date": format_date_fr_from_date(due_date),
            "source": f"BON DE COMMANDE N° {order.bon_de_commande}",
            "payment_reference": str(order.bon_de_commande),
            "payment_terms_days": int(company_info.get("payment_terms_days", 30)),
        },
        "company": company_info,
        "addresses": {
            "shipping_address": get_shipping_address_lines(order),
            "invoice_address": get_invoice_address_lines(order),
        },
        "items": items,
        "serial_items": serial_items,
        "totals": {
            "untaxed_amount_raw": untaxed_amount,
            "vat_raw": vat,
            "total_raw": total,
            "total_units_raw": total_units,
            "untaxed_amount": format_eur(untaxed_amount),
            "vat": format_eur(vat),
            "total": format_eur(total),
            "total_units": format_quantity(total_units),
        },
        "debug": {
            "order_id": order.id,
            "bon_de_commande": order.bon_de_commande,
            "shipment_batch_id": batch.id,
            "batch_number": batch.batch_number,
            "source_type": batch.source_type,
            "factory_confirmation_id": batch.factory_confirmation_id,
            "inventory_allocation_id": batch.inventory_allocation_id,
            "document_sequence": numbers,
        },
        "warnings": warnings + numbers.get("batch_numbering_warnings", []),
    }

    return invoice_data


def get_factory_for_batch(batch: ShipmentBatch):
    if batch.factory_confirmation_id:
        confirmation = batch.factory_confirmation
        if getattr(confirmation, "factory_id", None):
            return confirmation.factory

    if batch.order.factory_id:
        return batch.order.factory

    raise ValueError(
        f"Order {batch.order.bon_de_commande}: factory is missing. "
        "Cannot generate Factory PO."
    )


def build_batch_factory_po_items(
    batch: ShipmentBatch,
    factory_info: Dict[str, Any],
    shipping_date,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Factory PO 价格只来自 OrderItem 快照。

    Workflow 页面和 Factory PO 使用同一个
    Serial 级临期价格计算结果。
    """
    from workflow.services.batch_price_snapshot_service import (
        build_batch_price_snapshot,
    )

    snapshot = build_batch_price_snapshot(
        batch
    )

    if snapshot["errors"]:
        raise ValueError(
            "Factory PO price validation failed: "
            + "; ".join(
                snapshot["errors"][:10]
            )
        )

    prepared_factory = prepare_factory_info(
        factory_info
    )

    default_description = (
        prepared_factory.get(
            "default_product_description",
            "HT-Supreme™ Drug Eluting Stent",
        )
    )

    po_items: List[Dict[str, Any]] = []

    for group in snapshot["po_groups"]:
        quantity = Decimal(
            group["quantity"]
        )

        unit_price = Decimal(
            group["unit_price"]
        )

        discount_rate = Decimal(
            group["discount_rate"]
        )

        amount = Decimal(
            group["amount"]
        )

        min_expiration_date = (
            group.get(
                "min_expiration_date"
            )
        )

        discount_note = ""

        if discount_rate > 0:
            discount_note = (
                f"{float(discount_rate) * 100:.0f}% "
                "discount applied."
            )

            if min_expiration_date:
                discount_note += (
                    " Earliest expiration: "
                    f"{min_expiration_date.isoformat()}"
                )

            discount_note += (
                " Threshold: "
                f"{group['expiration_threshold_days']} "
                "days."
            )

        po_items.append(
            {
                "product_code": (
                    group["product_code"]
                ),
                "description": (
                    group["description"]
                    or default_description
                ),
                "quantity_raw": float(
                    quantity
                ),
                "unit_price_raw": float(
                    unit_price
                ),
                "discount_rate_raw": float(
                    discount_rate
                ),
                "final_unit_price_raw": float(
                    group[
                        "final_unit_price"
                    ]
                ),
                "amount_raw": float(
                    amount
                ),
                "quantity": (
                    format_po_quantity(
                        float(quantity)
                    )
                ),
                "unit_price": (
                    format_po_unit_price(
                        float(unit_price)
                    )
                ),
                "discount": (
                    format_po_discount(
                        float(discount_rate)
                    )
                ),
                "amount": format_po_eur(
                    float(amount)
                ),
                "discount_note": (
                    discount_note
                ),
                "serial_numbers": (
                    group["serial_numbers"]
                ),
                "expiration_dates": (
                    group["expiration_dates"]
                ),
                "min_expiration_date": (
                    min_expiration_date
                    .isoformat()
                    if min_expiration_date
                    else None
                ),
                "expiration_threshold_days": (
                    group[
                        "expiration_threshold_days"
                    ]
                ),
            }
        )

    if not po_items:
        raise ValueError(
            "No Factory PO item was generated "
            "from the current ShipmentBatch."
        )

    return (
        po_items,
        list(snapshot["warnings"]),
    )


def build_batch_factory_po_data(
    batch: ShipmentBatch,
    company_info: Dict[str, Any],
    factory_info: Dict[str, Any],
    numbers: Dict[str, Any],
) -> Dict[str, Any]:
    order = batch.order
    po_order_date, po_order_date_source, po_date_warnings = get_batch_po_order_date(batch)
    shipping_date = get_batch_shipping_date(batch)
    expected_arrival = shipping_date + timedelta(days=EXPECTED_ARRIVAL_DAYS)

    company = prepare_company_info(company_info)
    factory = prepare_factory_info(factory_info)

    items, warnings = build_batch_factory_po_items(
        batch=batch,
        factory_info=factory_info,
        shipping_date=shipping_date,
    )

    total_raw = sum(float(item.get("amount_raw") or 0) for item in items)

    po_data = {
        "po": {
            "po_number": numbers["po_number"],
            "order_date": format_date_display(po_order_date),
            "expected_arrival": format_date_display(expected_arrival),
            "order_date_iso": po_order_date.isoformat(),
            "shipping_date_iso": shipping_date.isoformat(),
            "expected_arrival_iso": expected_arrival.isoformat(),
        },
        "company": company,
        "factory": factory,
        "shipping_address": get_po_shipping_address_lines(order),
        "items": items,
        "totals": {
            "total_raw": total_raw,
            "total": format_po_eur(total_raw),
        },
        "debug": {
            "order_id": order.id,
            "bon_de_commande": order.bon_de_commande,
            "shipment_batch_id": batch.id,
            "batch_number": batch.batch_number,
            "source_type": batch.source_type,
            "factory_confirmation_id": batch.factory_confirmation_id,
            "inventory_allocation_id": batch.inventory_allocation_id,
            "document_sequence": numbers,
            "po_order_date_source": po_order_date_source,
            "discount_reference_date": shipping_date.isoformat(),
            "expiration_pricing_source": (
                "OrderItem price snapshot per product"
            ),
        },
        "warnings": warnings + po_date_warnings + numbers.get("batch_numbering_warnings", []),
    }

    return po_data


@transaction.atomic
def save_generated_document_record_for_batch(
    batch: ShipmentBatch,
    document_type: str,
    document_number: str,
    pdf_path: Path,
    html_path: Path,
    source_data: Dict[str, Any],
    generated_by,
) -> GeneratedDocument:
    obj, created = GeneratedDocument.objects.update_or_create(
        document_type=document_type,
        document_number=document_number,
        defaults={
            "order": batch.order,
            "shipment_batch": batch,
            "pdf_file": media_relative_path(pdf_path),
            "html_file": media_relative_path(html_path),
            "source_data": json_safe(source_data),
            "generated_by": generated_by,
        },
    )

    if obj.document_type in {
        GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE,
        GeneratedDocument
        .DocumentType
        .FACTORY_PO,
    }:
        from settlements.services.settlement_auto_service import (
            ensure_settlement_account_for_document,
        )

        ensure_settlement_account_for_document(
            obj
        )

    return obj


def generate_hospital_invoice_for_workflow_item(
    item: DocumentWorkflowItem,
    generated_by,
) -> Dict[str, Any]:
    batch = item.shipment_batch
    order = item.order

    numbers = get_batch_document_numbers(batch)

    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )

    invoice_data = build_batch_hospital_invoice_data(
        batch=batch,
        company_info=company_info,
        numbers=numbers,
    )

    workspace = (
        get_order_document_workspace(order)
        / "workflow_batches"
        / f"batch_{batch.id}"
        / "invoices"
    )

    filename_base = sanitize_filename(numbers["invoice_number"])

    html_path = workspace / f"{filename_base}.html"
    pdf_path = workspace / f"{filename_base}.pdf"
    data_path = workspace / f"{filename_base}_data.json"

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

    source_data = {
        "workflow_item_id": item.id,
        "shipment_batch_id": batch.id,
        "numbers": numbers,
        "invoice_data": invoice_data,
    }

    save_json_file(source_data, data_path)

    generated_document = save_generated_document_record_for_batch(
        batch=batch,
        document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
        document_number=numbers["invoice_number"],
        pdf_path=pdf_path,
        html_path=html_path,
        source_data=source_data,
        generated_by=generated_by,
    )

    return {
        "generated_document": generated_document,
        "generated_document_id": generated_document.id,
        "document_type": GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
        "document_number": numbers["invoice_number"],
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "data_path": str(data_path),
        "warnings": invoice_data.get("warnings", []),
    }


def generate_factory_po_for_workflow_item(
    item: DocumentWorkflowItem,
    generated_by,
) -> Dict[str, Any]:
    batch = item.shipment_batch
    order = item.order

    numbers = get_batch_document_numbers(batch)

    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )

    factory = get_factory_for_batch(batch)

    factory_info = build_factory_info_from_model(factory)

    po_data = build_batch_factory_po_data(
        batch=batch,
        company_info=company_info,
        factory_info=factory_info,
        numbers=numbers,
    )

    workspace = (
        get_order_document_workspace(order)
        / "workflow_batches"
        / f"batch_{batch.id}"
        / "purchase_orders"
    )

    filename_base = sanitize_filename(f"Purchase_Order_{numbers['po_number']}")

    html_path = workspace / f"{filename_base}.html"
    pdf_path = workspace / f"{filename_base}.pdf"
    data_path = workspace / f"{filename_base}_data.json"

    html_content = render_po_html(
        po_data=po_data,
        template_path=Path(settings.BASE_DIR)
        / "templates"
        / "factory_purchase_order.html",
    )

    write_po_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=Path(settings.BASE_DIR),
    )

    source_data = {
        "workflow_item_id": item.id,
        "shipment_batch_id": batch.id,
        "numbers": numbers,
        "po_data": po_data,
    }

    save_json_file(source_data, data_path)

    generated_document = save_generated_document_record_for_batch(
        batch=batch,
        document_type=GeneratedDocument.DocumentType.FACTORY_PO,
        document_number=numbers["po_number"],
        pdf_path=pdf_path,
        html_path=html_path,
        source_data=source_data,
        generated_by=generated_by,
    )

    return {
        "generated_document": generated_document,
        "generated_document_id": generated_document.id,
        "document_type": GeneratedDocument.DocumentType.FACTORY_PO,
        "document_number": numbers["po_number"],
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "data_path": str(data_path),
        "warnings": po_data.get("warnings", []),
    }


@transaction.atomic
def generate_documents_for_workflow_item(
    item: DocumentWorkflowItem,
    generated_by,
) -> Dict[str, Any]:
    item = (
        DocumentWorkflowItem.objects
        .select_for_update()
        .select_related("order", "shipment_batch")
        .get(id=item.id)
    )

    from workflow.services.workflow_validation_service import (
        validate_document_workflow_item,
    )

    validation_result = (
        validate_document_workflow_item(
            item=item,
            save=True,
        )
    )

    item.refresh_from_db()

    if not validation_result.get(
        "can_generate_documents"
    ):
        validation_errors = (
            validation_result.get("errors")
            or []
        )

        error_text = "; ".join(
            str(error)
            for error in validation_errors[:10]
        )

        raise ValueError(
            f"Workflow item {item.id} "
            "failed final validation. "
            f"{error_text}"
        )

    if (
        item.validation_status
        != DocumentWorkflowItem
        .ValidationStatus
        .READY
    ):
        raise ValueError(
            f"Workflow item {item.id} is not ready. "
            f"Current validation_status="
            f"{item.validation_status}."
        )

    if (
        item.invoice_status == DocumentWorkflowItem.DocumentStatus.GENERATED
        and item.po_status == DocumentWorkflowItem.DocumentStatus.GENERATED
    ):
        raise ValueError(
            f"Workflow item {item.id} already has generated Invoice and PO."
        )

    invoice_result = generate_hospital_invoice_for_workflow_item(
        item=item,
        generated_by=generated_by,
    )

    po_result = generate_factory_po_for_workflow_item(
        item=item,
        generated_by=generated_by,
    )

    item.invoice_status = DocumentWorkflowItem.DocumentStatus.GENERATED
    item.po_status = DocumentWorkflowItem.DocumentStatus.GENERATED
    item.workflow_status = DocumentWorkflowItem.WorkflowStatus.GENERATED
    item.invoice_document = invoice_result["generated_document"]
    item.po_document = po_result["generated_document"]

    item.save(
        update_fields=[
            "invoice_status",
            "po_status",
            "workflow_status",
            "invoice_document",
            "po_document",
            "updated_at",
        ]
    )

    return {
        "workflow_item_id": item.id,
        "order_id": item.order_id,
        "bon_de_commande": item.order.bon_de_commande,
        "batch_id": item.shipment_batch_id,
        "batch_number": item.shipment_batch.batch_number,
        "invoice": invoice_result,
        "factory_po": po_result,
    }


def generate_documents_for_workflow_items(queryset, generated_by):
    summary = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "results": [],
    }

    for item in queryset.select_related("order", "shipment_batch"):
        summary["processed"] += 1

        try:
            result = generate_documents_for_workflow_item(
                item=item,
                generated_by=generated_by,
            )

            summary["success"] += 1
            summary["results"].append(
                {
                    "success": True,
                    "workflow_item_id": item.id,
                    "bon_de_commande": result["bon_de_commande"],
                    "batch_number": result["batch_number"],
                    "invoice_number": result["invoice"]["document_number"],
                    "po_number": result["factory_po"]["document_number"],
                    "warnings": (
                        result["invoice"].get("warnings", [])
                        + result["factory_po"].get("warnings", [])
                    ),
                }
            )

        except Exception as exc:
            summary["failed"] += 1
            summary["results"].append(
                {
                    "success": False,
                    "workflow_item_id": item.id,
                    "bon_de_commande": item.order.bon_de_commande,
                    "batch_number": item.shipment_batch.batch_number,
                    "error": str(exc),
                }
            )

    return summary
