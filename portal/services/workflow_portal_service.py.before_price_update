from django.urls import reverse

from portal.services.common import (
    document_url,
    get_portal_lang,
    get_user_display_name,
)


def get_order_number(item):
    data = item.validation_data or {}

    if data.get("bon_de_commande"):
        return data.get("bon_de_commande")

    order = getattr(item, "order", None)

    if order is None:
        return f"Workflow #{item.id}"

    for field_name in ["bon_de_commande", "order_number", "number", "reference"]:
        value = getattr(order, field_name, None)
        if value:
            return value

    return f"Order #{getattr(order, 'id', item.id)}"


def get_hospital_name(item):
    order = getattr(item, "order", None)

    if order is None:
        return "—"

    for field_name in [
        "hospital_name",
        "shipping_hospital_name",
        "billing_hospital_name",
        "customer_name",
    ]:
        value = getattr(order, field_name, None)
        if value:
            return value

    hospital = getattr(order, "hospital", None)

    if hospital:
        for field_name in ["name", "hospital_name"]:
            value = getattr(hospital, field_name, None)
            if value:
                return value
        return str(hospital)

    return "—"


def status_label(value, kind):
    value = value or ""

    if kind == "validation":
        mapping = {
            "ready": ("已验证", "success"),
            "validated": ("已验证", "success"),
            "success": ("已验证", "success"),
            "ok": ("已验证", "success"),
            "pending": ("待验证", "warning"),
            "error": ("有问题", "danger"),
            "failed": ("有问题", "danger"),
            "blocked": ("有问题", "danger"),
        }
        return mapping.get(value, (value or "未知", "info"))

    if kind in ["invoice", "po"]:
        mapping = {
            "generated": ("已生成", "success"),
            "pending": ("待生成", "warning"),
            "missing": ("未生成", "warning"),
            "not_generated": ("未生成", "warning"),
            "failed": ("生成失败", "danger"),
        }
        return mapping.get(value, (value or "未生成", "warning"))

    if kind == "workflow":
        mapping = {
            "generated": ("文件已生成", "success"),
            "ready": ("可生成文件", "info"),
            "ready_to_generate": ("可生成文件", "info"),
            "pending": ("处理中", "warning"),
            "blocked": ("阻塞", "danger"),
            "cancelled": ("已取消", "warning"),
            "error": ("有问题", "danger"),
        }
        return mapping.get(value, (value or "未知", "info"))

    return value, "info"


def get_source_label(item):
    data = item.validation_data or {}
    source_type = data.get("source_type")

    if source_type == "factory_confirmation":
        return "工厂确认"

    if source_type == "inventory_allocation":
        return "库存分配"

    if source_type:
        return source_type

    return "—"


def get_next_action(item):
    validation_status = item.validation_status or ""
    invoice_status = item.invoice_status or ""
    po_status = item.po_status or ""

    if validation_status != "ready":
        return "检查验证结果", "warning"

    if invoice_status != "generated" and po_status != "generated":
        return "生成发票和采购订单", "info"

    if invoice_status != "generated":
        return "生成发票", "info"

    if po_status != "generated":
        return "生成采购订单", "info"

    return "已完成", "success"


def build_workflow_list_context(request):
    from workflow.models import DocumentWorkflowItem

    lang = get_portal_lang(request)

    query = (request.GET.get("q") or "").strip()
    status_filter = request.GET.get("status") or "all"

    items = list(
        DocumentWorkflowItem.objects.select_related(
            "shipment_batch",
            "order",
            "invoice_document",
            "po_document",
        )
        .order_by("-updated_at", "-id")[:300]
    )

    rows = []

    for item in items:
        order_number = get_order_number(item)
        hospital_name = get_hospital_name(item)
        next_action_text, next_action_class = get_next_action(item)

        validation_text, validation_class = status_label(item.validation_status, "validation")
        invoice_text, invoice_class = status_label(item.invoice_status, "invoice")
        po_text, po_class = status_label(item.po_status, "po")
        workflow_text, workflow_class = status_label(item.workflow_status, "workflow")

        row_status = "done"

        if item.validation_status != "ready":
            row_status = "pending"
        elif item.invoice_status != "generated" or item.po_status != "generated":
            row_status = "docs"

        data = item.validation_data or {}

        rows.append(
            {
                "id": item.id,
                "order_number": order_number,
                "hospital_name": hospital_name,
                "source_label": get_source_label(item),
                "batch_number": data.get("batch_number"),
                "workflow_text": workflow_text,
                "workflow_class": workflow_class,
                "validation_text": validation_text,
                "validation_class": validation_class,
                "invoice_text": invoice_text,
                "invoice_class": invoice_class,
                "po_text": po_text,
                "po_class": po_class,
                "invoice_url": document_url(item.invoice_document),
                "po_url": document_url(item.po_document),
                "next_action_text": next_action_text,
                "next_action_class": next_action_class,
                "admin_url": f"/admin/workflow/documentworkflowitem/{item.id}/change/",
                "detail_url": reverse("portal:workflow_detail", args=[item.id]),
                "action_url": reverse("portal:workflow_item_action", args=[item.id]),
                "can_generate": not (
                    item.invoice_status == "generated"
                    and item.po_status == "generated"
                ),
                "updated_at": item.updated_at,
                "row_status": row_status,
                "search_text": f"{order_number} {hospital_name}".lower(),
            }
        )

    unfiltered_rows = list(rows)

    if query:
        lowered_query = query.lower()
        rows = [row for row in rows if lowered_query in row["search_text"]]

    if status_filter == "pending":
        rows = [row for row in rows if row["row_status"] == "pending"]
    elif status_filter == "docs":
        rows = [row for row in rows if row["row_status"] == "docs"]
    elif status_filter == "done":
        rows = [row for row in rows if row["row_status"] == "done"]

    return {
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
        "rows": rows,
        "query": query,
        "status_filter": status_filter,
        "stats": {
            "all": len(unfiltered_rows),
            "pending": sum(1 for row in unfiltered_rows if row["row_status"] == "pending"),
            "docs": sum(1 for row in unfiltered_rows if row["row_status"] == "docs"),
            "done": sum(1 for row in unfiltered_rows if row["row_status"] == "done"),
        },
    }


def build_workflow_detail_context(request, item_id):
    from django.shortcuts import get_object_or_404
    from workflow.models import DocumentWorkflowItem

    lang = get_portal_lang(request)

    item = get_object_or_404(
        DocumentWorkflowItem.objects.select_related(
            "shipment_batch",
            "order",
            "invoice_document",
            "po_document",
        ),
        id=item_id,
    )

    data = item.validation_data or {}

    order_number = get_order_number(item)
    hospital_name = get_hospital_name(item)

    workflow_text, workflow_class = status_label(item.workflow_status, "workflow")
    validation_text, validation_class = status_label(item.validation_status, "validation")
    invoice_text, invoice_class = status_label(item.invoice_status, "invoice")
    po_text, po_class = status_label(item.po_status, "po")

    next_action_text, next_action_class = get_next_action(item)

    product_rows = []
    cumulative_detail = data.get("cumulative_order_detail") or {}

    for product_code, detail in sorted(cumulative_detail.items()):
        product_rows.append(
            {
                "product_code": product_code,
                "requested_quantity": detail.get("requested_quantity", 0),
                "confirmed_quantity": detail.get("order_item_confirmed_quantity", 0),
                "shipped_quantity": detail.get("cumulative_shipped_quantity", 0),
                "backordered_quantity": detail.get("expected_backordered_quantity", 0),
            }
        )

    batch_quantities = data.get("batch_quantities") or {}
    serial_counts = data.get("serial_counts") or {}

    batch_rows = []
    all_product_codes = sorted(set(batch_quantities.keys()) | set(serial_counts.keys()))

    for product_code in all_product_codes:
        batch_rows.append(
            {
                "product_code": product_code,
                "batch_quantity": batch_quantities.get(product_code, 0),
                "serial_count": serial_counts.get(product_code, 0),
            }
        )

    return {
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
        "item": item,
        "order_number": order_number,
        "hospital_name": hospital_name,
        "source_label": get_source_label(item),
        "batch_number": data.get("batch_number"),
        "workflow_text": workflow_text,
        "workflow_class": workflow_class,
        "validation_text": validation_text,
        "validation_class": validation_class,
        "invoice_text": invoice_text,
        "invoice_class": invoice_class,
        "po_text": po_text,
        "po_class": po_class,
        "invoice_url": document_url(item.invoice_document),
        "po_url": document_url(item.po_document),
        "next_action_text": next_action_text,
        "next_action_class": next_action_class,
        "product_rows": product_rows,
        "batch_rows": batch_rows,
        "errors": data.get("errors") or [],
        "warnings": data.get("warnings") or [],
        "can_generate_documents": data.get("can_generate_documents"),
        "admin_url": f"/admin/workflow/documentworkflowitem/{item.id}/change/",
        "action_url": reverse("portal:workflow_item_action", args=[item.id]),
    }
