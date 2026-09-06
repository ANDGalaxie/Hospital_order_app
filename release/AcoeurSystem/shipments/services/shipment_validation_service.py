from collections import Counter, defaultdict
from typing import Any, Dict, List

from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import SerialItem
from orders.models import Order
from shipments.models import ShipmentBatch, ShipmentBatchItem

try:
    from backorders.models import InventoryItem, BackorderLine
except Exception:
    InventoryItem = None
    BackorderLine = None


def get_quantity_from_batch_item(batch_item) -> int:
    """
    兼容不同字段名：
    - shipped_quantity
    - quantity
    """
    if hasattr(batch_item, "shipped_quantity"):
        return int(batch_item.shipped_quantity or 0)

    if hasattr(batch_item, "quantity"):
        return int(batch_item.quantity or 0)

    return 0


def get_batch_item_quantities(batch: ShipmentBatch) -> Counter:
    """
    读取当前 ShipmentBatch 里每个 product_code 的发货数量。
    """
    quantities = Counter()

    for item in ShipmentBatchItem.objects.filter(batch=batch):
        product_code = str(item.product_code or "").strip()
        quantity = get_quantity_from_batch_item(item)

        if product_code:
            quantities[product_code] += quantity

    return quantities


def get_order_item_map(order: Order) -> Dict[str, Any]:
    """
    product_code -> OrderItem
    """
    result = {}

    for item in order.items.all():
        product_code = str(item.product_code or "").strip()

        if product_code:
            result[product_code] = item

    return result


def get_cumulative_shipped_quantities(order: Order) -> Counter:
    """
    读取该订单所有 ShipmentBatchItem 的累计发货数量。
    """
    quantities = Counter()

    for item in ShipmentBatchItem.objects.filter(batch__order=order):
        product_code = str(item.product_code or "").strip()
        quantity = get_quantity_from_batch_item(item)

        if product_code:
            quantities[product_code] += quantity

    return quantities


def get_serial_rows_for_batch(batch: ShipmentBatch) -> List[Dict[str, Any]]:
    """
    根据 ShipmentBatch 来源，读取当前批次对应的 serial 明细。

    FactoryConfirmation 来源：
      SerialItem

    InventoryAllocation 来源：
      InventoryItem
    """
    rows = []

    if getattr(batch, "factory_confirmation_id", None):
        serial_items = SerialItem.objects.filter(
            order=batch.order,
            factory_confirmation=batch.factory_confirmation,
        )

        for serial in serial_items:
            rows.append(
                {
                    "source": "factory_confirmation",
                    "source_id": serial.id,
                    "product_code": str(serial.product_code or "").strip(),
                    "serial_number": str(serial.serial_number or "").strip(),
                    "expiration_date": serial.expiration_date,
                }
            )

    elif InventoryItem is not None and getattr(batch, "inventory_allocation_id", None):
        inventory_items = InventoryItem.objects.filter(
            allocation=batch.inventory_allocation,
        )

        for item in inventory_items:
            rows.append(
                {
                    "source": "inventory_allocation",
                    "source_id": item.id,
                    "product_code": str(item.product_code or "").strip(),
                    "serial_number": str(item.serial_number or "").strip(),
                    "expiration_date": item.expiration_date,
                    "inventory_status": item.status,
                    "allocated_order_id": item.allocated_order_id,
                }
            )

    return rows


def check_duplicate_serials_global(batch: ShipmentBatch, serial_rows: List[Dict[str, Any]]) -> List[str]:
    """
    检查当前 batch 的 serial 是否在其他发货来源中重复出现。

    这里做成 error，因为 serial 重复会导致实际库存 / 发货记录不可信。
    """
    errors = []

    for row in serial_rows:
        serial_number = row.get("serial_number")

        if not serial_number:
            continue

        # 当前 batch 是 FactoryConfirmation 来源时，排除自己这份 confirmation。
        qs_serial = SerialItem.objects.filter(serial_number=serial_number)

        if getattr(batch, "factory_confirmation_id", None):
            qs_serial = qs_serial.exclude(
                factory_confirmation=batch.factory_confirmation
            )

        if qs_serial.exists():
            errors.append(
                f"Serial Number {serial_number} 已经存在于其他 FactoryConfirmation 发货记录中。"
            )

        if InventoryItem is not None:
            qs_inventory = InventoryItem.objects.filter(serial_number=serial_number)

            if getattr(batch, "inventory_allocation_id", None):
                qs_inventory = qs_inventory.exclude(
                    allocation=batch.inventory_allocation
                )

            if qs_inventory.filter(status__in=["reserved", "allocated"]).exists():
                errors.append(
                    f"Serial Number {serial_number} 已经存在于其他库存预留 / 分配记录中。"
                )

    return errors


def validate_batch_source(batch: ShipmentBatch, errors: List[str], warnings: List[str]) -> None:
    """
    检查 ShipmentBatch 来源是否合理。
    """
    source_type = getattr(batch, "source_type", "")

    has_factory_confirmation = bool(getattr(batch, "factory_confirmation_id", None))
    has_inventory_allocation = bool(getattr(batch, "inventory_allocation_id", None))

    if has_factory_confirmation and has_inventory_allocation:
        errors.append(
            "ShipmentBatch 同时绑定了 FactoryConfirmation 和 InventoryAllocation，来源不唯一。"
        )
        return

    if not has_factory_confirmation and not has_inventory_allocation:
        errors.append(
            "ShipmentBatch 没有绑定 FactoryConfirmation 或 InventoryAllocation，无法确认发货来源。"
        )
        return

    if source_type == "factory_confirmation" and not has_factory_confirmation:
        errors.append(
            "source_type 是 factory_confirmation，但 factory_confirmation 为空。"
        )

    if source_type == "inventory_allocation" and not has_inventory_allocation:
        errors.append(
            "source_type 是 inventory_allocation，但 inventory_allocation 为空。"
        )

    if has_factory_confirmation:
        confirmation = batch.factory_confirmation

        if confirmation.order_id != batch.order_id:
            errors.append(
                "FactoryConfirmation 所属订单和 ShipmentBatch 所属订单不一致。"
            )

        if getattr(confirmation, "extraction_status", None) != "success":
            warnings.append(
                f"FactoryConfirmation 提取状态不是 success，当前为 {confirmation.extraction_status}。"
            )

    if has_inventory_allocation:
        allocation = batch.inventory_allocation

        if allocation.order_id != batch.order_id:
            errors.append(
                "InventoryAllocation 所属订单和 ShipmentBatch 所属订单不一致。"
            )

        if getattr(allocation, "status", None) != "shipment_created":
            warnings.append(
                f"InventoryAllocation 状态不是 shipment_created，当前为 {allocation.status}。"
            )


def validate_batch_items(
    batch: ShipmentBatch,
    order_item_map: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> Counter:
    """
    检查 ShipmentBatchItem。
    """
    batch_quantities = Counter()

    batch_items = list(
        ShipmentBatchItem.objects.filter(batch=batch)
    )

    if not batch_items:
        errors.append("ShipmentBatch 没有任何 ShipmentBatchItem。")
        return batch_quantities

    for item in batch_items:
        product_code = str(item.product_code or "").strip()
        quantity = get_quantity_from_batch_item(item)

        if not product_code:
            errors.append(f"ShipmentBatchItem {item.id}: 缺少 product_code。")
            continue

        if quantity <= 0:
            errors.append(
                f"ShipmentBatchItem {item.id} / {product_code}: 发货数量必须大于 0。"
            )

        if product_code not in order_item_map:
            errors.append(
                f"产品 {product_code} 出现在 ShipmentBatch 中，但不在医院订单产品列表里。"
            )

        batch_quantities[product_code] += quantity

    return batch_quantities


def validate_serial_rows(
    batch: ShipmentBatch,
    batch_quantities: Counter,
    order_item_map: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> Counter:
    """
    检查当前 batch 来源 serial 是否完整，并与 ShipmentBatchItem 数量一致。
    """
    serial_rows = get_serial_rows_for_batch(batch)

    if not serial_rows:
        errors.append(
            "当前 ShipmentBatch 找不到任何 Serial 明细。"
        )
        return Counter()

    serial_counts = Counter()
    serial_numbers = []

    for row in serial_rows:
        product_code = row.get("product_code")
        serial_number = row.get("serial_number")
        expiration_date = row.get("expiration_date")

        label = f"{product_code or 'NO_PRODUCT'} / {serial_number or 'NO_SERIAL'}"

        if not product_code:
            errors.append(f"Serial 明细 {label}: 缺少 product_code。")

        if not serial_number:
            errors.append(f"Serial 明细 {label}: 缺少 serial_number。")

        if not expiration_date:
            errors.append(f"Serial 明细 {label}: 缺少 expiration_date。")

        if product_code and product_code not in order_item_map:
            errors.append(
                f"Serial 明细 {label}: 产品号不在医院订单产品列表里。"
            )

        if row.get("source") == "inventory_allocation":
            if row.get("inventory_status") != "allocated":
                warnings.append(
                    f"库存 Serial {label}: 状态不是 allocated，当前为 {row.get('inventory_status')}。"
                )

            if row.get("allocated_order_id") != batch.order_id:
                errors.append(
                    f"库存 Serial {label}: allocated_order 和 ShipmentBatch order 不一致。"
                )

        if product_code:
            serial_counts[product_code] += 1

        if serial_number:
            serial_numbers.append(serial_number)

    duplicate_in_batch = [
        serial for serial, count in Counter(serial_numbers).items()
        if count > 1
    ]

    if duplicate_in_batch:
        errors.append(
            "当前 ShipmentBatch 内部发现重复 Serial Number："
            + ", ".join(duplicate_in_batch)
        )

    for product_code, shipped_qty in batch_quantities.items():
        serial_count = serial_counts.get(product_code, 0)

        if shipped_qty != serial_count:
            errors.append(
                f"产品 {product_code}: ShipmentBatchItem 数量={shipped_qty}，"
                f"但 Serial 数量={serial_count}。"
            )

    errors.extend(
        check_duplicate_serials_global(
            batch=batch,
            serial_rows=serial_rows,
        )
    )

    return serial_counts


def validate_cumulative_order_quantities(
    batch: ShipmentBatch,
    order_item_map: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> Dict[str, Any]:
    """
    检查订单累计发货数量是否合理。
    """
    cumulative = get_cumulative_shipped_quantities(batch.order)

    detail = {}

    for product_code, order_item in order_item_map.items():
        requested = int(order_item.requested_quantity or 0)
        confirmed_snapshot = int(order_item.confirmed_quantity or 0)
        backordered_snapshot = int(order_item.backordered_quantity or 0)

        cumulative_shipped = int(cumulative.get(product_code, 0))
        expected_backordered = max(requested - cumulative_shipped, 0)

        detail[product_code] = {
            "requested_quantity": requested,
            "cumulative_shipped_quantity": cumulative_shipped,
            "order_item_confirmed_quantity": confirmed_snapshot,
            "order_item_backordered_quantity": backordered_snapshot,
            "expected_backordered_quantity": expected_backordered,
        }

        if cumulative_shipped > requested:
            errors.append(
                f"产品 {product_code}: 累计已发数量 {cumulative_shipped} "
                f"大于医院订单数量 {requested}，存在超发。"
            )

        if confirmed_snapshot != cumulative_shipped:
            warnings.append(
                f"产品 {product_code}: OrderItem.confirmed_quantity={confirmed_snapshot}，"
                f"但所有 ShipmentBatch 累计数量={cumulative_shipped}。"
            )

        if backordered_snapshot != expected_backordered:
            warnings.append(
                f"产品 {product_code}: OrderItem.backordered_quantity={backordered_snapshot}，"
                f"但按累计发货计算应为 {expected_backordered}。"
            )

        if BackorderLine is not None:
            line = BackorderLine.objects.filter(
                order=batch.order,
                product_code=product_code,
            ).first()

            if line:
                line_remaining = int(line.remaining_quantity or 0)

                if line.is_active and line_remaining != expected_backordered:
                    warnings.append(
                        f"产品 {product_code}: BackorderLine.remaining_quantity={line_remaining}，"
                        f"但按累计发货计算应为 {expected_backordered}。"
                    )

    extra_shipped_codes = [
        code for code in cumulative.keys()
        if code not in order_item_map
    ]

    if extra_shipped_codes:
        errors.append(
            "ShipmentBatch 累计发货中出现医院订单没有的产品："
            + ", ".join(extra_shipped_codes)
        )

    return detail


@transaction.atomic
def validate_shipment_batch(
    batch: ShipmentBatch,
    save: bool = True,
) -> Dict[str, Any]:
    """
    校验单个 ShipmentBatch 是否适合生成 Invoice / PO。
    """
    batch = (
        ShipmentBatch.objects
        .select_for_update()
        .select_related("order")
        .get(id=batch.id)
    )

    errors: List[str] = []
    warnings: List[str] = []

    if not batch.order_id:
        errors.append("ShipmentBatch 没有关联 Order。")

    if not batch.batch_number:
        errors.append("ShipmentBatch 缺少 batch_number。")

    if not batch.batch_date:
        errors.append("ShipmentBatch 缺少 batch_date。")

    order_item_map = get_order_item_map(batch.order)

    if not order_item_map:
        errors.append("当前 Order 没有 OrderItem。")

    validate_batch_source(
        batch=batch,
        errors=errors,
        warnings=warnings,
    )

    batch_quantities = validate_batch_items(
        batch=batch,
        order_item_map=order_item_map,
        errors=errors,
        warnings=warnings,
    )

    serial_counts = validate_serial_rows(
        batch=batch,
        batch_quantities=batch_quantities,
        order_item_map=order_item_map,
        errors=errors,
        warnings=warnings,
    )

    cumulative_detail = validate_cumulative_order_quantities(
        batch=batch,
        order_item_map=order_item_map,
        errors=errors,
        warnings=warnings,
    )

    if errors:
        validation_status = ShipmentBatch.ValidationStatus.BLOCKED
    elif warnings:
        validation_status = ShipmentBatch.ValidationStatus.NEEDS_REVIEW
    else:
        validation_status = ShipmentBatch.ValidationStatus.READY

    result = {
        "can_generate_documents": len(errors) == 0,
        "validation_status": validation_status,
        "batch_id": batch.id,
        "order_id": batch.order_id,
        "bon_de_commande": batch.order.bon_de_commande if batch.order_id else None,
        "batch_number": batch.batch_number,
        "source_type": getattr(batch, "source_type", None),
        "factory_confirmation_id": getattr(batch, "factory_confirmation_id", None),
        "inventory_allocation_id": getattr(batch, "inventory_allocation_id", None),
        "batch_quantities": dict(batch_quantities),
        "serial_counts": dict(serial_counts),
        "cumulative_order_detail": cumulative_detail,
        "errors": errors,
        "warnings": warnings,
        "checked_at": timezone.now().isoformat(),
    }

    if save:
        batch.validation_status = validation_status
        batch.validation_data = result
        batch.validated_at = timezone.now()
        batch.save(
            update_fields=[
                "validation_status",
                "validation_data",
                "validated_at",
                "updated_at",
            ]
        )

    return result


def validate_order_shipments(
    order: Order,
    save: bool = False,
) -> Dict[str, Any]:
    """
    校验一个订单下所有 ShipmentBatch。

    Phase 3C 先主要用于检查，不强行写入 Order。
    """
    batch_results = []

    for batch in ShipmentBatch.objects.filter(order=order).order_by("batch_number", "id"):
        batch_results.append(
            validate_shipment_batch(batch, save=save)
        )

    errors = []
    warnings = []

    for result in batch_results:
        errors.extend(
            [
                f"Batch {result['batch_number']}: {message}"
                for message in result.get("errors", [])
            ]
        )
        warnings.extend(
            [
                f"Batch {result['batch_number']}: {message}"
                for message in result.get("warnings", [])
            ]
        )

    if errors:
        validation_status = "blocked"
    elif warnings:
        validation_status = "needs_review"
    else:
        validation_status = "ready"

    return {
        "order_id": order.id,
        "bon_de_commande": order.bon_de_commande,
        "validation_status": validation_status,
        "batch_count": len(batch_results),
        "errors": errors,
        "warnings": warnings,
        "batch_results": batch_results,
        "checked_at": timezone.now().isoformat(),
    }
