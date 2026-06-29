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
    """
    从所有 ShipmentBatchItem 累计更新 OrderItem。

    OrderItem.confirmed_quantity = 累计已发数量
    OrderItem.backordered_quantity = requested_quantity - 累计已发数量
    """
    if warnings is None:
        warnings = []

    shipped_map = Counter()

    batch_items = (
        ShipmentBatchItem.objects
        .filter(batch__order=order)
        .select_related("batch")
    )

    for batch_item in batch_items:
        product_code = str(batch_item.product_code or "").strip()
        shipped_quantity = get_quantity_from_batch_item(batch_item)

        if product_code:
            shipped_map[product_code] += shipped_quantity

    order_product_codes = set(
        order.items.values_list("product_code", flat=True)
    )

    extra_codes = [
        code for code in shipped_map.keys()
        if code not in order_product_codes
    ]

    if extra_codes:
        warnings.append(
            "发货批次中出现医院订单里没有的产品，需要人工确认："
            + ", ".join(extra_codes)
        )

    for order_item in order.items.all():
        product_code = str(order_item.product_code or "").strip()
        requested_quantity = int(order_item.requested_quantity or 0)
        confirmed_quantity = int(shipped_map.get(product_code, 0))

        backordered_quantity = max(
            requested_quantity - confirmed_quantity,
            0,
        )

        order_item.confirmed_quantity = confirmed_quantity
        order_item.backordered_quantity = backordered_quantity

        if confirmed_quantity <= 0:
            order_item.status = OrderItem.Status.BACKORDERED

        elif confirmed_quantity < requested_quantity:
            order_item.status = OrderItem.Status.PARTIALLY_CONFIRMED

        else:
            order_item.status = OrderItem.Status.CONFIRMED

        if confirmed_quantity > requested_quantity:
            warnings.append(
                f"产品 {product_code}: 累计已发数量 {confirmed_quantity} "
                f"大于医院订单数量 {requested_quantity}，可能存在超发。"
            )

        order_item.save(
            update_fields=[
                "confirmed_quantity",
                "backordered_quantity",
                "status",
                "updated_at",
            ]
        )

    return warnings


def update_batch_summary(batch):
    """
    更新 ShipmentBatch 的汇总字段。
    如果某些字段不存在，会自动跳过。
    """
    shipped_total = 0

    for item in ShipmentBatchItem.objects.filter(batch=batch):
        shipped_total += get_quantity_from_batch_item(item)

    remaining_total = 0

    for order_item in batch.order.items.all():
        remaining_total += int(order_item.backordered_quantity or 0)

    update_fields = []

    if hasattr(batch, "shipped_this_batch_quantity"):
        batch.shipped_this_batch_quantity = shipped_total
        update_fields.append("shipped_this_batch_quantity")

    if hasattr(batch, "remaining_after_batch_quantity"):
        batch.remaining_after_batch_quantity = remaining_total
        update_fields.append("remaining_after_batch_quantity")

    if hasattr(batch, "status"):
        # 尽量兼容不同 Status 命名。
        if remaining_total == 0:
            if hasattr(ShipmentBatch, "Status") and hasattr(ShipmentBatch.Status, "COMPLETE"):
                batch.status = ShipmentBatch.Status.COMPLETE
            elif hasattr(ShipmentBatch, "Status") and hasattr(ShipmentBatch.Status, "COMPLETED"):
                batch.status = ShipmentBatch.Status.COMPLETED
            else:
                batch.status = "complete"
        else:
            if hasattr(ShipmentBatch, "Status") and hasattr(ShipmentBatch.Status, "PARTIAL"):
                batch.status = ShipmentBatch.Status.PARTIAL
            elif hasattr(ShipmentBatch, "Status") and hasattr(ShipmentBatch.Status, "PARTIALLY_SHIPPED"):
                batch.status = ShipmentBatch.Status.PARTIALLY_SHIPPED
            else:
                batch.status = "partial"

        update_fields.append("status")

    if update_fields:
        batch.save(update_fields=update_fields)


def rebuild_backorder_snapshot_for_batch(batch):
    """
    创建这个 ShipmentBatch 之后的待发快照。

    快照含义：
      当前 batch 发完之后，每个 OrderItem 还剩多少。
    """
    if BackorderSnapshotItem is None:
        return

    BackorderSnapshotItem.objects.filter(batch=batch).delete()

    field_names = get_model_field_names(BackorderSnapshotItem)

    for order_item in batch.order.items.all():
        kwargs = {
            "batch": batch,
            "order": batch.order,
            "product": order_item.product,
            "product_code": order_item.product_code,
            "description": order_item.description,
            "requested_quantity": order_item.requested_quantity,
            "confirmed_quantity": order_item.confirmed_quantity,
            "remaining_quantity": order_item.backordered_quantity,
            "backordered_quantity": order_item.backordered_quantity,
        }

        safe_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in field_names
        }

        BackorderSnapshotItem.objects.create(**safe_kwargs)


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

    warnings = []

    recalculate_order_items_from_shipment_batches(
        order=order,
        warnings=warnings,
    )

    update_batch_summary(batch)
    rebuild_backorder_snapshot_for_batch(batch)

    if sync_backorders_for_order:
        sync_backorders_for_order(order)

    rebuild_inventory_product_folders()

    return batch
