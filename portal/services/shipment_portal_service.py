from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse

from portal.services.common import get_portal_lang, get_user_display_name
from portal.services.workflow_portal_service import status_label
from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


def build_shipment_list_context(request):
    query = (request.GET.get("q") or "").strip()
    source_type = request.GET.get("source_type") or "all"
    validation_status = request.GET.get("validation_status") or "all"
    status = request.GET.get("status") or "all"

    qs = ShipmentBatch.objects.select_related(
        "order",
        "factory_confirmation",
        "inventory_allocation",
    ).order_by("-batch_date", "-id")

    if query:
        qs = qs.filter(
            Q(order__bon_de_commande__icontains=query)
            | Q(batch_number__icontains=query)
        )

    if source_type != "all":
        qs = qs.filter(source_type=source_type)

    if validation_status != "all":
        qs = qs.filter(validation_status=validation_status)

    if status != "all":
        qs = qs.filter(status=status)

    workflow_map = {
        item.shipment_batch_id: item
        for item in DocumentWorkflowItem.objects.filter(
            shipment_batch_id__in=qs.values_list("id", flat=True)
        )
    }

    rows = []

    for batch in qs[:300]:
        workflow_item = workflow_map.get(batch.id)
        workflow_text, workflow_class = status_label(
            workflow_item.workflow_status if workflow_item else "",
            "workflow",
        )

        rows.append(
            {
                "id": batch.id,
                "batch_number": batch.batch_number,
                "batch_date": batch.batch_date,
                "order_number": batch.order.bon_de_commande,
                "hospital_name": batch.order.hospital_name or "—",
                "source_type": batch.source_type,
                "confirmation_type": (
                    batch.factory_confirmation.get_confirmation_type_display()
                    if batch.factory_confirmation_id
                    else "库存补发"
                ),
                "shipped_quantity": batch.shipped_this_batch_quantity,
                "remaining_quantity": batch.remaining_after_batch_quantity,
                "validation_status": batch.validation_status,
                "status": batch.status,
                "workflow_text": workflow_text,
                "workflow_class": workflow_class,
                "detail_url": reverse("portal:shipment_detail", args=[batch.id]),
            }
        )

    return {
        "lang": get_portal_lang(request),
        "user_display_name": get_user_display_name(request.user),
        "rows": rows,
        "query": query,
        "source_type": source_type,
        "validation_status": validation_status,
        "status": status,
        "source_type_choices": ShipmentBatch.SourceType.choices,
        "validation_status_choices": ShipmentBatch.ValidationStatus.choices,
        "status_choices": ShipmentBatch.Status.choices,
    }


def build_shipment_detail_context(request, batch_id):
    batch = get_object_or_404(
        ShipmentBatch.objects.select_related(
            "order",
            "factory_confirmation",
            "inventory_allocation",
        ),
        id=batch_id,
    )

    workflow_item = (
        DocumentWorkflowItem.objects.select_related(
            "invoice_document",
            "po_document",
        ).filter(shipment_batch=batch).first()
    )

    return {
        "lang": get_portal_lang(request),
        "user_display_name": get_user_display_name(request.user),
        "batch": batch,
        "workflow_item": workflow_item,
        "shipped_items": batch.shipped_items.order_by("product_code", "id"),
        "backorder_items": batch.backorder_items.order_by("product_code", "id"),
        "generated_documents": batch.generated_documents.order_by("-generated_at", "-id"),
        "order_url": reverse("portal:order_detail", args=[batch.order_id]),
        "factory_confirmation_url": (
            reverse("portal:factory_detail", args=[batch.factory_confirmation_id])
            if batch.factory_confirmation_id
            else ""
        ),
        "workflow_url": (
            reverse("portal:workflow_detail", args=[workflow_item.id])
            if workflow_item
            else ""
        ),
        "admin_url": f"/admin/shipments/shipmentbatch/{batch.id}/change/",
    }
