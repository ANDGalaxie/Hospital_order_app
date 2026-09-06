from collections import Counter
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from backorders.models import (
    BackorderLine,
    InventoryAllocation,
    InventoryItem,
)
from backorders.services.inventory_service import rebuild_inventory_product_folders
from orders.models import OrderItem
from shipments.models import ShipmentBatch, ShipmentBatchItem
from shipments.services.shipment_history_service import (
    rebuild_order_shipment_history,
)

try:
    from shipments.models import BackorderSnapshotItem
except Exception:
    BackorderSnapshotItem = None

try:
    from shipments.services.shipment_tracking_service import (
        get_or_create_order_shipment_folder,
    )
except Exception:
    get_or_create_order_shipment_folder = None

try:
    from backorders.services.backorder_sync_service import sync_backorders_for_order
except Exception:
    sync_backorders_for_order = None

try:
    from workflow.services.workflow_sync_service import (
        sync_document_workflow_item_for_batch,
    )
except Exception:
    sync_document_workflow_item_for_batch = None

try:
    from workflow.services.workflow_validation_service import (
        validate_document_workflow_item,
    )
except Exception:
    validate_document_workflow_item = None


def get_model_field_names(model):
    return {field.name for field in model._meta.fields}


def filter_model_kwargs(model, kwargs):
    field_names = get_model_field_names(model)
    return {
        key: value
        for key, value in kwargs.items()
        if key in field_names
    }


def get_next_batch_number(order):
    max_number = (
        ShipmentBatch.objects
        .filter(order=order)
        .aggregate(v=Max("batch_number"))
        .get("v")
    )

    return int(max_number or 0) + 1


def get_quantity_from_batch_item(batch_item):
    if hasattr(batch_item, "shipped_quantity"):
        return int(batch_item.shipped_quantity or 0)

    if hasattr(batch_item, "quantity"):
        return int(batch_item.quantity or 0)

    return 0


def recalculate_order_items_from_shipment_batches(order, warnings=None):
    if warnings is None:
        warnings = []
    result = rebuild_order_shipment_history(order)
    warnings.extend(result.get("warnings") or [])
    return warnings


def update_batch_summary(batch):
    rebuild_order_shipment_history(batch.order)


def rebuild_backorder_snapshot_for_batch(batch):
    rebuild_order_shipment_history(batch.order)


def validate_inventory_allocation_before_shipment(allocation):
    """
    生成 ShipmentBatch 前的严格检查。
    """
    if allocation.status != InventoryAllocation.Status.RESERVED:
        raise ValueError(
            f"库存预留记录 {allocation.id} 状态不是“已预留”，不能生成发货批次。"
        )

    if hasattr(allocation, "shipment_batch"):
        raise ValueError(
            f"库存预留记录 {allocation.id} 已经生成过 ShipmentBatch。"
        )

    order = allocation.order
    product_code = str(allocation.product_code or "").strip()

    reserved_items = list(
        allocation.items
        .select_for_update()
        .filter(status=InventoryItem.Status.RESERVED)
        .order_by("expiration_date", "serial_number")
    )

    if not reserved_items:
        raise ValueError(
            f"库存预留记录 {allocation.id} 没有 reserved 状态的 Serial。"
        )

    if len(reserved_items) != int(allocation.allocated_count or 0):
        raise ValueError(
            f"库存预留记录 {allocation.id} 的 allocated_count="
            f"{allocation.allocated_count}，但 reserved Serial 数量="
            f"{len(reserved_items)}，需要人工检查。"
        )

    for item in reserved_items:
        if item.allocated_order_id != order.id:
            raise ValueError(
                f"Serial {item.serial_number} 的 allocated_order "
                f"不是 Order {order.bon_de_commande}，不能生成发货批次。"
            )

        if item.product_code != product_code:
            raise ValueError(
                f"Serial {item.serial_number} 的 product_code={item.product_code}，"
                f"和 allocation product_code={product_code} 不一致。"
            )

    backorder_line = (
        BackorderLine.objects
        .select_for_update()
        .filter(
            order=order,
            product_code=product_code,
            is_active=True,
            remaining_quantity__gt=0,
        )
        .first()
    )

    if not backorder_line:
        raise ValueError(
            f"Order {order.bon_de_commande} 当前没有待发产品 {product_code}。"
        )

    remaining_quantity = int(backorder_line.remaining_quantity or 0)

    if len(reserved_items) > remaining_quantity:
        raise ValueError(
            f"预留数量 {len(reserved_items)} 超过当前待发数量 "
            f"{remaining_quantity}。请先取消或调整预留。"
        )

    return reserved_items


@transaction.atomic
def create_shipment_batch_from_inventory_allocation(allocation):
    """
    从库存预留记录生成正式补发 ShipmentBatch。

    这是 Phase 3B 的核心函数。
    """
    allocation = (
        InventoryAllocation.objects
        .select_for_update()
        .select_related("order")
        .get(id=allocation.id)
    )

    reserved_items = validate_inventory_allocation_before_shipment(
        allocation
    )

    order = allocation.order
    product_code = allocation.product_code
    quantity = len(reserved_items)

    batch_date = timezone.localdate()
    batch_number = get_next_batch_number(order)

    month = None
    order_folder = None

    if get_or_create_order_shipment_folder:
        month, order_folder = get_or_create_order_shipment_folder(order)

    batch_kwargs = {
        "order": order,
        "batch_number": batch_number,
        "batch_date": batch_date,
        "source_type": ShipmentBatch.SourceType.INVENTORY_ALLOCATION,
        "inventory_allocation": allocation,
        "factory_confirmation": None,
        "month": month,
        "order_folder": order_folder,
    }

    batch = ShipmentBatch.objects.create(
        **filter_model_kwargs(ShipmentBatch, batch_kwargs)
    )

    description = ""

    if allocation.product:
        description = allocation.product.description or ""

    item_kwargs = {
        "batch": batch,
        "product": allocation.product,
        "product_code": product_code,
        "description": description,
        "shipped_quantity": quantity,
        "quantity": quantity,
    }

    ShipmentBatchItem.objects.create(
        **filter_model_kwargs(ShipmentBatchItem, item_kwargs)
    )

    now = timezone.now()

    for item in reserved_items:
        item.status = InventoryItem.Status.ALLOCATED
        item.allocated_order = order
        item.allocated_at = now
        item.save(
            update_fields=[
                "status",
                "allocated_order",
                "allocated_at",
            ]
        )

    allocation.status = InventoryAllocation.Status.SHIPMENT_CREATED
    allocation.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    rebuild_order_shipment_history(order)

    if sync_backorders_for_order:
        sync_backorders_for_order(order)

    rebuild_inventory_product_folders()

    if sync_document_workflow_item_for_batch is None:
        raise ValueError(
            "Workflow 同步服务不可用，不能为库存补发创建 WorkflowItem。"
        )

    workflow_sync_result = sync_document_workflow_item_for_batch(batch)

    if isinstance(workflow_sync_result, tuple):
        workflow_item = workflow_sync_result[0]
    else:
        workflow_item = workflow_sync_result

    if workflow_item is None:
        raise ValueError(
            f"ShipmentBatch {batch.id} 已创建，但没有成功创建 WorkflowItem。"
        )

    if validate_document_workflow_item is None:
        raise ValueError(
            "Workflow 校验服务不可用，不能完成库存补发工作流校验。"
        )

    validate_document_workflow_item(
        workflow_item,
        save=True,
    )

    return batch
