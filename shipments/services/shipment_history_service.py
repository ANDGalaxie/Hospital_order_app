from collections import Counter
from copy import deepcopy

from django.db import transaction

from orders.models import Order, OrderItem
from shipments.models import (
    BackorderSnapshotItem,
    ShipmentBatch,
    ShipmentBatchItem,
)


def calculate_batch_status(total_remaining, has_over_shipped, shipped_this_batch):
    if has_over_shipped:
        return ShipmentBatch.Status.OVER_SHIPPED

    if shipped_this_batch <= 0:
        return ShipmentBatch.Status.NEEDS_REVIEW

    if total_remaining > 0:
        return ShipmentBatch.Status.PARTIAL

    return ShipmentBatch.Status.COMPLETE


def get_order_shipment_batches_in_history_order(order):
    return list(
        ShipmentBatch.objects
        .select_for_update()
        .filter(order=order)
        .order_by("batch_date", "created_at", "id")
    )


def summarize_order_shipment_history(order):
    batches = list(
        ShipmentBatch.objects
        .filter(order=order)
        .order_by("batch_date", "created_at", "id")
    )

    return {
        "order_id": order.id,
        "bon_de_commande": order.bon_de_commande,
        "batch_count": len(batches),
        "batches": [
            {
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "source_type": batch.source_type,
                "batch_date": batch.batch_date.isoformat() if batch.batch_date else None,
                "shipped_this_batch_quantity": int(batch.shipped_this_batch_quantity or 0),
                "total_shipped_after_batch_quantity": int(
                    batch.total_shipped_after_batch_quantity or 0
                ),
                "remaining_after_batch_quantity": int(
                    batch.remaining_after_batch_quantity or 0
                ),
                "status": batch.status,
            }
            for batch in batches
        ],
        "order_items": [
            {
                "product_code": item.product_code,
                "requested_quantity": int(item.requested_quantity or 0),
                "confirmed_quantity": int(item.confirmed_quantity or 0),
                "backordered_quantity": int(item.backordered_quantity or 0),
                "status": item.status,
            }
            for item in order.items.order_by("id")
        ],
    }


@transaction.atomic
def rebuild_order_shipment_history(order):
    order = (
        Order.objects
        .select_for_update()
        .get(id=order.id)
    )

    batches = get_order_shipment_batches_in_history_order(order)
    order_items = list(order.items.all().order_by("id"))

    requested_by_product = Counter()
    product_map = {}
    total_requested_quantity = 0

    for order_item in order_items:
        product_code = str(order_item.product_code or "").strip()
        requested_qty = int(order_item.requested_quantity or 0)
        requested_by_product[product_code] += requested_qty
        product_map[product_code] = order_item
        total_requested_quantity += requested_qty

    all_batch_items = list(
        ShipmentBatchItem.objects
        .filter(batch__order=order)
        .select_related("batch")
        .order_by("batch__batch_date", "batch__created_at", "batch__id", "id")
    )

    batch_items_by_batch_id = {}
    for batch in batches:
        batch_items_by_batch_id[batch.id] = []

    for batch_item in all_batch_items:
        batch_items_by_batch_id.setdefault(
            batch_item.batch_id,
            [],
        ).append(batch_item)

    BackorderSnapshotItem.objects.filter(
        batch__order=order,
    ).delete()

    cumulative_shipped_by_product = Counter()
    total_shipped_running = 0
    batch_summaries = []

    for batch in batches:
        batch_items = batch_items_by_batch_id.get(batch.id, [])
        batch_counter = Counter()
        shipped_this_batch_quantity = 0

        for batch_item in batch_items:
            product_code = str(batch_item.product_code or "").strip()
            shipped_qty = int(batch_item.shipped_quantity or 0)
            batch_counter[product_code] += shipped_qty
            shipped_this_batch_quantity += shipped_qty

        cumulative_before = deepcopy(cumulative_shipped_by_product)

        for product_code, shipped_qty in batch_counter.items():
            cumulative_shipped_by_product[product_code] += shipped_qty

        total_shipped_running += shipped_this_batch_quantity

        remaining_after_batch_quantity = 0
        has_over_shipped = False

        snapshot_rows = []

        for order_item in order_items:
            product_code = str(order_item.product_code or "").strip()
            requested_qty = int(order_item.requested_quantity or 0)
            shipped_before = int(cumulative_before.get(product_code, 0))
            shipped_this_batch = int(batch_counter.get(product_code, 0))
            shipped_after = int(cumulative_shipped_by_product.get(product_code, 0))
            remaining_qty_raw = requested_qty - shipped_after
            remaining_qty = max(remaining_qty_raw, 0)
            is_over_shipped = remaining_qty_raw < 0

            if is_over_shipped:
                has_over_shipped = True

            remaining_after_batch_quantity += remaining_qty

            snapshot_rows.append(
                BackorderSnapshotItem(
                    batch=batch,
                    product=order_item.product,
                    product_code=product_code,
                    requested_quantity=requested_qty,
                    shipped_before_batch_quantity=shipped_before,
                    shipped_this_batch_quantity=shipped_this_batch,
                    total_shipped_after_batch_quantity=shipped_after,
                    remaining_quantity=remaining_qty,
                    is_over_shipped=is_over_shipped,
                )
            )

        if snapshot_rows:
            BackorderSnapshotItem.objects.bulk_create(snapshot_rows)

        batch.total_requested_quantity = total_requested_quantity
        batch.shipped_this_batch_quantity = shipped_this_batch_quantity
        batch.total_shipped_after_batch_quantity = total_shipped_running
        batch.remaining_after_batch_quantity = remaining_after_batch_quantity
        batch.status = calculate_batch_status(
            total_remaining=remaining_after_batch_quantity,
            has_over_shipped=has_over_shipped,
            shipped_this_batch=shipped_this_batch_quantity,
        )
        batch.save(
            update_fields=[
                "total_requested_quantity",
                "shipped_this_batch_quantity",
                "total_shipped_after_batch_quantity",
                "remaining_after_batch_quantity",
                "status",
                "updated_at",
            ]
        )

        batch_summaries.append(
            {
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "shipped_this_batch_quantity": shipped_this_batch_quantity,
                "total_shipped_after_batch_quantity": total_shipped_running,
                "remaining_after_batch_quantity": remaining_after_batch_quantity,
                "status": batch.status,
            }
        )

    warnings = []

    for order_item in order_items:
        product_code = str(order_item.product_code or "").strip()
        requested_quantity = int(order_item.requested_quantity or 0)
        confirmed_quantity = int(cumulative_shipped_by_product.get(product_code, 0))
        backordered_quantity = max(requested_quantity - confirmed_quantity, 0)

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

    return {
        "order_id": order.id,
        "bon_de_commande": order.bon_de_commande,
        "batch_count": len(batches),
        "batches": batch_summaries,
        "warnings": warnings,
        "final_total_shipped_quantity": total_shipped_running,
        "final_remaining_quantity": sum(
            int(item.backordered_quantity or 0)
            for item in order_items
        ),
    }
