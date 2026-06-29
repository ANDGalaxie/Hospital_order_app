from django.db import transaction

from shipments.models import ShipmentBatch
from workflow.models import DocumentWorkflowItem


@transaction.atomic
def sync_document_workflow_item_for_batch(batch):
    """
    确保一个 ShipmentBatch 有对应的 DocumentWorkflowItem。
    """
    item, created = DocumentWorkflowItem.objects.get_or_create(
        shipment_batch=batch,
        defaults={
            "order": batch.order,
        },
    )

    if item.order_id != batch.order_id:
        item.order = batch.order
        item.save(update_fields=["order", "updated_at"])

    return item, created


@transaction.atomic
def sync_document_workflow_items_for_all_batches():
    """
    扫描所有 ShipmentBatch，为没有 workflow item 的 batch 创建待处理文件。
    """
    created_count = 0
    updated_count = 0

    batches = (
        ShipmentBatch.objects
        .select_related("order")
        .order_by("order__bon_de_commande", "batch_number", "id")
    )

    for batch in batches:
        item, created = sync_document_workflow_item_for_batch(batch)

        if created:
            created_count += 1
        else:
            updated_count += 1

    return {
        "created": created_count,
        "updated": updated_count,
        "total": created_count + updated_count,
    }
