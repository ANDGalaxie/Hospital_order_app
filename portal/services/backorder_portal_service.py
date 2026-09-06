from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse

from backorders.models import BackorderLine, InventoryAllocation, InventoryItem
from backorders.services.inventory_service import allocate_inventory_to_order
from backorders.services.inventory_shipment_service import (
    create_shipment_batch_from_inventory_allocation,
)
from portal.services.common import get_portal_lang, get_user_display_name
from shipments.models import ShipmentBatch


def backorder_status_label(value):
    return {
        BackorderLine.Status.OPEN: "待安排",
        BackorderLine.Status.PLANNED: "已计划",
        BackorderLine.Status.OVERDUE: "已逾期",
        BackorderLine.Status.COMPLETED: "已完成",
    }.get(value, value or "—")


def build_backorder_queryset(params):
    query = (params.get("q") or "").strip()
    status = params.get("status") or "active"
    hospital = (params.get("hospital") or "").strip()

    qs = BackorderLine.objects.select_related(
        "order",
        "order__hospital",
        "product",
    ).order_by("order__bon_de_commande", "product_code", "id")

    if status == "active":
        qs = qs.filter(is_active=True, remaining_quantity__gt=0)
    elif status != "all":
        qs = qs.filter(status=status)

    if hospital:
        qs = qs.filter(order__hospital_name__icontains=hospital)

    if query:
        qs = qs.filter(
            Q(order__bon_de_commande__icontains=query)
            | Q(product_code__icontains=query)
        )

    return qs


def build_backorder_list_context(request):
    query = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or "active"
    hospital = (request.GET.get("hospital") or "").strip()
    qs = build_backorder_queryset(request.GET)

    rows = []
    for line in qs[:300]:
        rows.append(
            {
                "id": line.id,
                "order": line.order,
                "product_code": line.product_code,
                "requested_quantity": line.requested_quantity,
                "shipped_quantity": line.shipped_quantity,
                "remaining_quantity": line.remaining_quantity,
                "expected_shipping_date": line.expected_shipping_date,
                "status_text": backorder_status_label(line.status),
                "is_active_text": "是" if line.is_active else "否",
            }
        )

    export_params = request.GET.copy()
    export_params.pop("page", None)

    return {
        "lang": get_portal_lang(request),
        "user_display_name": get_user_display_name(request.user),
        "rows": rows,
        "query": query,
        "status_filter": status,
        "hospital_filter": hospital,
        "status_choices": BackorderLine.Status.choices,
        "export_query_string": export_params.urlencode(),
    }


def build_backorder_detail_context(request, backorder_id):
    line = get_object_or_404(
        BackorderLine.objects.select_related(
            "order",
            "order__hospital",
            "product",
        ),
        id=backorder_id,
    )

    related_batches = ShipmentBatch.objects.filter(
        order=line.order,
        shipped_items__product_code=line.product_code,
    ).distinct().order_by("-batch_number", "-id")

    related_allocations = InventoryAllocation.objects.filter(
        order=line.order,
        product_code=line.product_code,
    ).order_by("-created_at", "-id")

    available_inventory_count = InventoryItem.objects.filter(
        product_code=line.product_code,
        status=InventoryItem.Status.AVAILABLE,
    ).count()

    return {
        "lang": get_portal_lang(request),
        "user_display_name": get_user_display_name(request.user),
        "line": line,
        "line_status_text": backorder_status_label(line.status),
        "related_batches": related_batches,
        "related_allocations": related_allocations,
        "available_inventory_count": available_inventory_count,
        "upload_replenishment_factory_url": reverse(
            "portal:order_factory_upload",
            args=[line.order_id],
        ) + "?type=replenishment",
        "order_url": reverse("portal:order_detail", args=[line.order_id]),
    }


def reserve_inventory_for_backorder(
    *,
    backorder_id,
    quantity_requested,
    user,
):
    line = BackorderLine.objects.select_related("order").get(id=backorder_id)
    return allocate_inventory_to_order(
        product_code=line.product_code,
        order=line.order,
        quantity=quantity_requested,
        created_by=user,
    )


def create_inventory_shipment_for_allocation(
    *,
    allocation_id,
):
    allocation = InventoryAllocation.objects.get(id=allocation_id)
    return create_shipment_batch_from_inventory_allocation(
        allocation
    )
