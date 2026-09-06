from collections import Counter

from django.db import transaction

from backorders.models import InventoryItem
from factory_confirmations.models import SerialItem
from shipments.models import ShipmentBatchItem
from shipments.services.shipment_tracking_service import (
    sync_shipment_batch_from_factory_confirmation,
)
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_sync_service import (
    sync_document_workflow_item_for_batch,
)
from workflow.services.backorder_document_regeneration_service import (
    BACKORDER_SOURCE_TYPES,
    get_backorder_batch_reason,
    iter_backorder_batches,
)


def _batch_item_counts(batch):
    return Counter(
        {
            str(product_code): int(quantity or 0)
            for product_code, quantity in (
                ShipmentBatchItem.objects
                .filter(batch=batch)
                .values_list("product_code", "shipped_quantity")
            )
        }
    )


def _factory_serial_counts(batch):
    return Counter(
        str(product_code)
        for product_code in (
            SerialItem.objects
            .filter(
                order=batch.order,
                factory_confirmation=batch.factory_confirmation,
            )
            .values_list("product_code", flat=True)
        )
        if product_code
    )


def audit_backorder_history_batch(batch):
    errors = []
    actions = []
    workflow_items = list(
        DocumentWorkflowItem.objects.filter(
            shipment_batch=batch
        ).order_by("id")
    )
    batch_items = _batch_item_counts(batch)

    if batch.source_type not in BACKORDER_SOURCE_TYPES:
        errors.append("source_type 不是支持的补发来源。")
    if not batch.order_id:
        errors.append("ShipmentBatch 缺少 Order。")
    if not batch_items:
        errors.append("ShipmentBatch 没有 ShipmentBatchItem。")
    if len(workflow_items) > 1:
        errors.append(
            "ShipmentBatch 存在多个 WorkflowItem，拒绝自动选择。"
        )

    if batch.inventory_allocation_id:
        allocation = batch.inventory_allocation
        if not allocation:
            errors.append("inventory_allocation 关联记录不存在。")
        else:
            inventory_items = list(
                InventoryItem.objects.filter(
                    allocation=allocation
                ).select_related("batch")
            )
            if not inventory_items:
                errors.append("InventoryAllocation 没有关联 Serial。")
            if allocation.status != allocation.Status.SHIPMENT_CREATED:
                errors.append(
                    "InventoryAllocation 状态不是 shipment_created。"
                )
            if int(allocation.allocated_count or 0) != len(inventory_items):
                errors.append(
                    "InventoryAllocation.allocated_count 与关联 Serial 数量不一致。"
                )
            for item in inventory_items:
                if item.allocated_order_id != batch.order_id:
                    errors.append(
                        f"Serial {item.serial_number} 不属于当前 Order。"
                    )
                if item.product_code != allocation.product_code:
                    errors.append(
                        f"Serial {item.serial_number} 的产品编号与 InventoryAllocation 不一致。"
                    )

    if batch.factory_confirmation_id:
        confirmation = batch.factory_confirmation
        if not confirmation:
            errors.append("FactoryConfirmation 关联记录不存在。")
        else:
            if not confirmation.shipping_date:
                errors.append("FactoryConfirmation.shipping_date 为空。")
            serial_counts = _factory_serial_counts(batch)
            if not serial_counts:
                errors.append("FactoryConfirmation 没有 SerialItem。")
            if batch_items != serial_counts:
                errors.append(
                    "现有 ShipmentBatchItem 数量与 FactoryConfirmation Serial 数量不一致，拒绝自动重建。"
                )
            if (
                batch.batch_date
                and confirmation.shipping_date
                and batch.batch_date != confirmation.shipping_date
            ):
                created_date = (
                    batch.created_at.date()
                    if batch.created_at
                    else None
                )
                if created_date != batch.batch_date:
                    errors.append(
                        "batch_date 与 shipping_date 不一致，且无法确认 batch_date 只是创建日期。"
                    )
                else:
                    actions.append("sync_factory_confirmation_batch_date")

    if not workflow_items:
        actions.append("create_workflow_item")

    return {
        "batch_id": batch.id,
        "order_id": batch.order_id,
        "bon_de_commande": (
            batch.order.bon_de_commande
            if batch.order_id
            else ""
        ),
        "source_type": batch.source_type,
        "batch_number": batch.batch_number,
        "backorder_reason": get_backorder_batch_reason(batch),
        "workflow_item_id": (
            workflow_items[0].id
            if len(workflow_items) == 1
            else None
        ),
        "factory_confirmation_id": batch.factory_confirmation_id,
        "batch_date": batch.batch_date,
        "shipping_date": (
            batch.factory_confirmation.shipping_date
            if batch.factory_confirmation_id
            and batch.factory_confirmation
            else None
        ),
        "actions": actions,
        "errors": list(dict.fromkeys(errors)),
        "can_repair": not errors,
    }


def _repair_one_batch(batch, audit):
    workflow_created = False
    date_repaired = False

    if "create_workflow_item" in audit["actions"]:
        _, workflow_created = sync_document_workflow_item_for_batch(batch)

    if "sync_factory_confirmation_batch_date" in audit["actions"]:
        synced_batch = sync_shipment_batch_from_factory_confirmation(
            batch.factory_confirmation
        )
        if synced_batch.id != batch.id:
            raise ValueError(
                f"FactoryConfirmation 同步返回了错误的 ShipmentBatch ID={synced_batch.id}。"
            )
        synced_batch.batch_date = batch.factory_confirmation.shipping_date
        synced_batch.month_key = synced_batch.batch_date.strftime("%Y-%m")
        synced_batch.save(update_fields=["batch_date", "month_key"])
        date_repaired = True

    workflow_item_count = (
        DocumentWorkflowItem.objects
        .filter(shipment_batch_id=batch.id)
        .count()
    )
    if workflow_item_count != 1:
        raise ValueError(
            f"ShipmentBatch {batch.id} 修复后 WorkflowItem 数量={workflow_item_count}，预期为 1。"
        )

    batch.refresh_from_db()
    return {
        "batch_id": batch.id,
        "workflow_created": workflow_created,
        "date_repaired": date_repaired,
        "workflow_item_id": batch.document_workflow_item.id,
        "batch_date": batch.batch_date,
    }


def repair_backorder_history_batches(batches=None, apply=False):
    if batches is None:
        batches = list(iter_backorder_batches())
    else:
        batches = list(batches)

    audits = [
        audit_backorder_history_batch(batch)
        for batch in batches
    ]
    blockers = [
        audit
        for audit in audits
        if not audit["can_repair"]
    ]

    if not apply:
        return {
            "audits": audits,
            "blockers": blockers,
            "results": [],
        }

    if blockers:
        raise ValueError(
            "存在无法安全修复的历史批次："
            + ", ".join(
                str(audit["batch_id"])
                for audit in blockers
            )
        )

    results = []
    with transaction.atomic():
        for batch, audit in zip(batches, audits):
            results.append(_repair_one_batch(batch, audit))

    return {
        "audits": audits,
        "blockers": [],
        "results": results,
    }
