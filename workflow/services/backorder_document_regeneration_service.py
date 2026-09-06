import json
from typing import Any, Dict, Iterable

from django.db import transaction

from documents.models import GeneratedDocument
from orders.services.order_date_service import (
    parse_business_date,
)
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem
from workflow.services.batch_price_snapshot_service import (
    build_batch_price_snapshot,
)
from workflow.services.workflow_document_generation_service import (
    build_batch_invoice_items,
    generate_factory_po_for_workflow_item,
    generate_hospital_invoice_for_workflow_item,
    get_batch_shipping_date,
    get_factory_for_batch,
)

try:
    from backorders.models import InventoryItem
except Exception:
    InventoryItem = None


INVOICE = "invoice"
PURCHASE_ORDER = "purchase_order"
ALL = "all"
BACKORDER_SOURCE_TYPES = (
    ShipmentBatch.SourceType.FACTORY_CONFIRMATION,
    ShipmentBatch.SourceType.INVENTORY_ALLOCATION,
)


def load_dict(value):
    data = value or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return {}

    return data if isinstance(data, dict) else {}


def extract_date_from_mapping(data):
    if not isinstance(data, dict):
        return None

    factory_document = (
        data.get("factory_document")
        or {}
    )

    candidates = [
        data.get("factory_shipping_date"),
        data.get("shipping_date"),
        data.get("shipping_date_only_iso"),
    ]

    if isinstance(factory_document, dict):
        candidates.extend(
            [
                factory_document.get(
                    "shipping_date_only_iso"
                ),
                factory_document.get(
                    "factory_shipping_date"
                ),
                factory_document.get(
                    "shipping_date"
                ),
                factory_document.get("date"),
            ]
        )

    for value in candidates:
        parsed = parse_business_date(value)
        if parsed:
            return parsed

    return None


def get_existing_document(
    workflow_item,
    document_type,
):
    if (
        document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
        and workflow_item.invoice_document_id
    ):
        return workflow_item.invoice_document

    if (
        document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_PO
        and workflow_item.po_document_id
    ):
        return workflow_item.po_document

    return (
        GeneratedDocument.objects.filter(
            shipment_batch=(
                workflow_item.shipment_batch
            ),
            document_type=document_type,
        )
        .order_by("id")
        .first()
    )


def extract_current_price_basis(
    document,
    basis_key,
):
    if not document:
        return None

    source_data = load_dict(
        document.source_data
    )
    pricing_basis = load_dict(
        source_data.get("pricing_basis")
    )

    value = pricing_basis.get(basis_key)

    if value:
        return str(value)

    if basis_key == "hospital_order_date":
        rows = pricing_basis.get("rows") or []
        if rows and isinstance(rows[0], dict):
            value = rows[0].get(
                "hospital_order_date"
            )
            if value:
                return str(value)

    if basis_key == "factory_shipping_date":
        po_data = load_dict(
            source_data.get("po_data")
        )
        po = load_dict(po_data.get("po"))
        debug = load_dict(po_data.get("debug"))
        value = (
            po.get("shipping_date_iso")
            or debug.get(
                "discount_reference_date"
            )
        )
        if value:
            return str(value)

    return None


def resolve_inventory_factory_shipping_date(
    batch,
    existing_po=None,
    manual_date=None,
) -> Dict[str, Any]:
    result = {
        "date": None,
        "source": "",
        "errors": [],
    }

    if (
        not batch.inventory_allocation_id
        or InventoryItem is None
    ):
        result["errors"].append(
            "ShipmentBatch 不是 inventory_allocation 来源。"
        )
        return result

    items = list(
        InventoryItem.objects.filter(
            allocation=batch.inventory_allocation
        ).select_related("batch")
    )

    inventory_dates = {}

    for item in items:
        inventory_batch = item.batch
        item_date = inventory_batch.batch_date
        item_date_source = (
            "inventory_item.inventory_batch."
            "batch_date"
        )

        if not item_date:
            item_date = extract_date_from_mapping(
                load_dict(
                    inventory_batch.extracted_data
                )
            )
            item_date_source = (
                "inventory_item.inventory_batch."
                "extracted_data"
            )

        if not item_date:
            item_date = extract_date_from_mapping(
                load_dict(item.raw_data)
            )
            item_date_source = (
                "inventory_item.raw_data"
            )

        if item_date:
            inventory_dates.setdefault(
                item_date,
                set(),
            ).add(item_date_source)

    if len(inventory_dates) > 1:
        result["errors"].append(
            "当前库存补发批次的 Serial 对应多个不同"
            "工厂实际发货日期，无法自动选择。"
        )
        return result

    if len(inventory_dates) == 1:
        item_date, sources = next(
            iter(inventory_dates.items())
        )
        source_priority = (
            "inventory_item.inventory_batch."
            "batch_date",
            "inventory_item.inventory_batch."
            "extracted_data",
            "inventory_item.raw_data",
        )
        result["date"] = item_date
        result["source"] = next(
            source
            for source in source_priority
            if source in sources
        )
        return result

    allocation = batch.inventory_allocation

    for attr in (
        "factory_shipping_date",
        "shipping_date",
        "batch_date",
    ):
        parsed = parse_business_date(
            getattr(allocation, attr, None)
        )
        if parsed:
            result["date"] = parsed
            result["source"] = (
                f"inventory_allocation.{attr}"
            )
            return result

    current_basis = extract_current_price_basis(
        existing_po,
        "factory_shipping_date",
    )
    parsed_current_basis = (
        parse_business_date(current_basis)
    )

    if parsed_current_basis:
        result["date"] = parsed_current_basis
        result["source"] = (
            "generated_document.source_data."
            "pricing_basis"
        )
        return result

    if manual_date:
        result["date"] = manual_date
        result["source"] = (
            "command_line."
            "factory_shipping_date"
        )
        return result

    result["errors"].append(
        "当前库存补发批次没有可用的工厂实际发货日期。"
        "请使用 --factory-shipping-date YYYY-MM-DD 指定。"
    )
    return result


def normalize_document_types(
    document_type,
) -> Iterable[str]:
    if document_type == ALL:
        return (INVOICE, PURCHASE_ORDER)

    return (document_type,)


def get_backorder_batch_reason(batch):
    if batch.source_type not in BACKORDER_SOURCE_TYPES:
        return "source_type 不是支持的补发来源。"

    if batch.inventory_allocation_id:
        return "inventory_allocation 来源。"

    confirmation = (
        batch.factory_confirmation
        if batch.factory_confirmation_id
        else None
    )

    if (
        confirmation
        and confirmation.confirmation_type
        == "replenishment"
    ):
        return "FactoryConfirmation.confirmation_type=replenishment。"

    if int(batch.batch_number or 0) > 1:
        return "现有后续发货批次规则：batch_number > 1。"

    return None


def is_backorder_batch(batch):
    return bool(get_backorder_batch_reason(batch)) and (
        batch.source_type in BACKORDER_SOURCE_TYPES
    )


def iter_backorder_batches():
    batches = (
        ShipmentBatch.objects
        .filter(source_type__in=BACKORDER_SOURCE_TYPES)
        .select_related(
            "order",
            "factory_confirmation",
            "inventory_allocation",
        )
        .order_by("order_id", "batch_number", "id")
    )

    return (
        batch
        for batch in batches
        if is_backorder_batch(batch)
    )


def build_regeneration_preview(
    batch,
    document_type=ALL,
    manual_factory_shipping_date=None,
) -> Dict[str, Any]:
    requested_documents = list(
        normalize_document_types(
            document_type
        )
    )
    blockers = []

    try:
        workflow_item = (
            batch.document_workflow_item
        )
    except DocumentWorkflowItem.DoesNotExist:
        workflow_item = None
        blockers.append(
            "ShipmentBatch 没有现有 WorkflowItem；"
            "本命令不会创建新的 WorkflowItem。"
        )

    backorder_reason = get_backorder_batch_reason(
        batch
    )
    replenishment = is_backorder_batch(
        batch
    )

    if not replenishment:
        blockers.append(
            "目标 ShipmentBatch 不是补发批次。"
        )

    invoice_document = (
        get_existing_document(
            workflow_item,
            GeneratedDocument
            .DocumentType
            .HOSPITAL_INVOICE,
        )
        if workflow_item
        else None
    )
    po_document = (
        get_existing_document(
            workflow_item,
            GeneratedDocument
            .DocumentType
            .FACTORY_PO,
        )
        if workflow_item
        else None
    )

    preview = {
        "batch": batch,
        "batch_id": batch.id,
        "bon_de_commande": (
            batch.order.bon_de_commande
        ),
        "source_type": batch.source_type,
        "backorder_reason": backorder_reason,
        "batch_number": batch.batch_number,
        "is_replenishment": replenishment,
        "order_date": batch.order.order_date,
        "requested_documents": (
            requested_documents
        ),
        "invoice_document": invoice_document,
        "po_document": po_document,
        "invoice_document_id": (
            invoice_document.id
            if invoice_document
            else None
        ),
        "po_document_id": (
            po_document.id
            if po_document
            else None
        ),
        "missing_invoice": invoice_document is None,
        "missing_po": po_document is None,
        "invoice_current_basis": (
            extract_current_price_basis(
                invoice_document,
                "hospital_order_date",
            )
        ),
        "invoice_new_basis": (
            batch.order.order_date.isoformat()
            if batch.order.order_date
            else None
        ),
        "po_current_basis": (
            extract_current_price_basis(
                po_document,
                "factory_shipping_date",
            )
        ),
        "po_new_basis": None,
        "po_new_prices": [],
        "factory_shipping_date": None,
        "factory_shipping_date_source": "",
        "factory": None,
        "blockers": blockers,
        "can_execute": False,
    }

    if INVOICE in requested_documents:
        if not batch.order.order_date:
            blockers.append(
                "Order.order_date 为空，"
                "不能重新生成 Invoice。"
            )
        else:
            try:
                get_batch_shipping_date(batch)
                build_batch_invoice_items(
                    batch
                )
            except Exception as exc:
                blockers.append(str(exc))

    if PURCHASE_ORDER in requested_documents:
        if batch.factory_confirmation_id:
            confirmation = (
                batch.factory_confirmation
            )
            factory_shipping_date = (
                confirmation.shipping_date
            )
            date_source = (
                "factory_confirmation."
                "shipping_date"
            )

            if not factory_shipping_date:
                blockers.append(
                    "FactoryConfirmation.shipping_date "
                    "为空，不能重新生成 Factory PO。"
                )

        elif batch.inventory_allocation_id:
            date_result = (
                resolve_inventory_factory_shipping_date(
                    batch=batch,
                    existing_po=po_document,
                    manual_date=(
                        manual_factory_shipping_date
                    ),
                )
            )
            factory_shipping_date = (
                date_result["date"]
            )
            date_source = (
                date_result["source"]
            )
            blockers.extend(
                date_result["errors"]
            )

        else:
            factory_shipping_date = None
            date_source = ""
            blockers.append(
                "ShipmentBatch 没有支持的补发来源。"
            )

        preview["factory_shipping_date"] = (
            factory_shipping_date
        )
        preview[
            "factory_shipping_date_source"
        ] = date_source
        preview["po_new_basis"] = (
            factory_shipping_date.isoformat()
            if factory_shipping_date
            else None
        )

        if factory_shipping_date:
            try:
                effective_factory = (
                    get_factory_for_batch(
                        batch
                    )
                )
                preview["factory"] = (
                    effective_factory
                )
                snapshot = (
                    build_batch_price_snapshot(
                        batch,
                        factory_shipping_date=(
                            factory_shipping_date
                        ),
                        factory=effective_factory,
                        factory_shipping_date_source=(
                            date_source
                        ),
                    )
                )
                blockers.extend(
                    snapshot["errors"]
                )
                preview["po_new_prices"] = [
                    {
                        "product_code": group.get(
                            "product_code"
                        ),
                        "base_unit_price": str(
                            group.get("unit_price")
                        ),
                        "final_unit_price": str(
                            group.get("final_unit_price")
                        ),
                        "discount_rate": str(
                            group.get("discount_rate")
                        ),
                    }
                    for group in snapshot.get(
                        "po_groups",
                        [],
                    )
                ]
            except Exception as exc:
                blockers.append(str(exc))

    # 保持输出稳定，同时去除重复阻断信息。
    preview["blockers"] = list(
        dict.fromkeys(
            str(message)
            for message in blockers
            if str(message)
        )
    )
    preview["can_execute"] = (
        len(preview["blockers"]) == 0
    )
    return preview


@transaction.atomic
def regenerate_backorder_documents(
    batch,
    document_type=ALL,
    manual_factory_shipping_date=None,
) -> Dict[str, Any]:
    batch = (
        ShipmentBatch.objects
        .select_for_update()
        .get(id=batch.id)
    )
    workflow_item = (
        DocumentWorkflowItem.objects
        .select_for_update()
        .get(shipment_batch=batch)
    )
    preview = build_regeneration_preview(
        batch=batch,
        document_type=document_type,
        manual_factory_shipping_date=(
            manual_factory_shipping_date
        ),
    )

    if not preview["can_execute"]:
        raise ValueError(
            "; ".join(preview["blockers"])
        )

    existing_documents = [
        document
        for document in (
            preview["invoice_document"],
            preview["po_document"],
        )
        if document is not None
    ]
    generated_by = (
        existing_documents[0].generated_by
        if existing_documents
        else batch.order.created_by
    )
    results = {}

    if INVOICE in preview[
        "requested_documents"
    ]:
        invoice_result = (
            generate_hospital_invoice_for_workflow_item(
                item=workflow_item,
                generated_by=generated_by,
                force_regenerate=True,
                existing_document_override=(
                    preview[
                        "invoice_document"
                    ]
                ),
            )
        )
        workflow_item.invoice_document = (
            invoice_result[
                "generated_document"
            ]
        )
        workflow_item.invoice_status = (
            DocumentWorkflowItem
            .DocumentStatus
            .GENERATED
        )
        results[INVOICE] = invoice_result

    if PURCHASE_ORDER in preview[
        "requested_documents"
    ]:
        po_result = (
            generate_factory_po_for_workflow_item(
                item=workflow_item,
                generated_by=generated_by,
                force_regenerate=True,
                factory_shipping_date=(
                    preview[
                        "factory_shipping_date"
                    ]
                ),
                factory_shipping_date_source=(
                    preview[
                        "factory_shipping_date_source"
                    ]
                ),
                factory=preview["factory"],
                existing_document_override=(
                    preview["po_document"]
                ),
            )
        )
        workflow_item.po_document = (
            po_result["generated_document"]
        )
        workflow_item.po_status = (
            DocumentWorkflowItem
            .DocumentStatus
            .GENERATED
        )
        results[PURCHASE_ORDER] = po_result

    if (
        workflow_item.invoice_status
        == DocumentWorkflowItem
        .DocumentStatus
        .GENERATED
        and workflow_item.po_status
        == DocumentWorkflowItem
        .DocumentStatus
        .GENERATED
    ):
        workflow_item.workflow_status = (
            DocumentWorkflowItem
            .WorkflowStatus
            .GENERATED
        )

    workflow_item.save(
        update_fields=[
            "invoice_document",
            "invoice_status",
            "po_document",
            "po_status",
            "workflow_status",
            "updated_at",
        ]
    )

    return {
        "preview": preview,
        "results": results,
        "workflow_item": workflow_item,
    }
