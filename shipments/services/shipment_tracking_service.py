from collections import Counter

from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import SerialItem
from shipments.models import (
    ShipmentMonth,
    ShipmentOrderFolder,
    ShipmentBatch,
    ShipmentBatchItem,
    BackorderSnapshotItem,
)


def get_batch_date(confirmation):
    """
    发货批次日期：
    优先使用工厂确认文件 shipping_date。
    如果没有，则使用今天。
    """
    return confirmation.shipping_date or timezone.localdate()


def get_month_key(batch_date):
    return batch_date.strftime("%Y-%m")


def get_next_batch_number(order):
    last_batch = (
        ShipmentBatch.objects
        .filter(order=order)
        .order_by("-batch_number")
        .first()
    )

    if not last_batch:
        return 1

    return int(last_batch.batch_number) + 1


def build_shipped_map_from_serial_items(order, confirmation):
    """
    从 SerialItem 统计本批已发产品数量。
    """
    serial_items = SerialItem.objects.filter(
        order=order,
        factory_confirmation=confirmation,
    )

    counter = Counter()

    for serial in serial_items:
        if serial.product_code:
            counter[serial.product_code] += 1

    return counter


def build_previous_shipped_map(order, exclude_batch=None):
    """
    统计这个订单在之前所有批次里已经发过多少。
    """
    qs = ShipmentBatchItem.objects.filter(
        batch__order=order,
    )

    if exclude_batch is not None:
        qs = qs.exclude(batch=exclude_batch)

    counter = Counter()

    for item in qs:
        counter[item.product_code] += int(item.shipped_quantity or 0)

    return counter


def calculate_batch_status(total_remaining, has_over_shipped, shipped_this_batch):
    if has_over_shipped:
        return ShipmentBatch.Status.OVER_SHIPPED

    if shipped_this_batch <= 0:
        return ShipmentBatch.Status.NEEDS_REVIEW

    if total_remaining > 0:
        return ShipmentBatch.Status.PARTIAL

    return ShipmentBatch.Status.COMPLETE


@transaction.atomic
def sync_shipment_batch_from_factory_confirmation(confirmation):
    """
    根据一个 FactoryConfirmation 创建或更新发货批次。

    每次重新 extract 工厂确认文件后，可以重新运行这个函数。
    它会删除旧的 shipped/backorder 记录，再重新计算。
    """
    order = confirmation.order

    batch_date = get_batch_date(confirmation)
    month_key = get_month_key(batch_date)

    month, _ = ShipmentMonth.objects.get_or_create(
        month_key=month_key,
    )

    order_folder, _ = ShipmentOrderFolder.objects.get_or_create(
        month=month,
        order=order,
    )

    batch, created = ShipmentBatch.objects.get_or_create(
        factory_confirmation=confirmation,
        defaults={
            "order": order,
            "order_folder": order_folder,
            "batch_number": get_next_batch_number(order),
            "batch_date": batch_date,
            "month_key": month_key,
        },
    )

    batch.order = order
    batch.order_folder = order_folder
    batch.batch_date = batch_date
    batch.month_key = month_key

    # 如果是旧 batch，不要随便改 batch_number。
    if not batch.batch_number:
        batch.batch_number = get_next_batch_number(order)

    batch.shipped_items.all().delete()
    batch.backorder_items.all().delete()

    shipped_this_batch_map = build_shipped_map_from_serial_items(
        order=order,
        confirmation=confirmation,
    )

    previous_shipped_map = build_previous_shipped_map(
        order=order,
        exclude_batch=batch,
    )

    # 创建本批已发记录
    for product_code, qty in shipped_this_batch_map.items():
        order_item = order.items.filter(product_code=product_code).first()
        product = order_item.product if order_item else None

        ShipmentBatchItem.objects.create(
            batch=batch,
            product=product,
            product_code=product_code,
            shipped_quantity=int(qty),
        )

    total_requested = 0
    shipped_this_batch_total = 0
    total_shipped_after_batch_total = 0
    remaining_total = 0
    has_over_shipped = False

    # 创建待发快照
    for order_item in order.items.all().order_by("id"):
        product_code = order_item.product_code
        requested = int(order_item.requested_quantity or 0)

        shipped_before = int(previous_shipped_map.get(product_code, 0))
        shipped_this_batch = int(shipped_this_batch_map.get(product_code, 0))
        total_shipped_after = shipped_before + shipped_this_batch

        remaining = requested - total_shipped_after
        is_over_shipped = remaining < 0

        if is_over_shipped:
            has_over_shipped = True

        display_remaining = max(remaining, 0)

        BackorderSnapshotItem.objects.create(
            batch=batch,
            product=order_item.product,
            product_code=product_code,
            requested_quantity=requested,
            shipped_before_batch_quantity=shipped_before,
            shipped_this_batch_quantity=shipped_this_batch,
            total_shipped_after_batch_quantity=total_shipped_after,
            remaining_quantity=display_remaining,
            is_over_shipped=is_over_shipped,
        )

        total_requested += requested
        shipped_this_batch_total += shipped_this_batch
        total_shipped_after_batch_total += total_shipped_after
        remaining_total += display_remaining

    batch.total_requested_quantity = total_requested
    batch.shipped_this_batch_quantity = shipped_this_batch_total
    batch.total_shipped_after_batch_quantity = total_shipped_after_batch_total
    batch.remaining_after_batch_quantity = remaining_total
    batch.status = calculate_batch_status(
        total_remaining=remaining_total,
        has_over_shipped=has_over_shipped,
        shipped_this_batch=shipped_this_batch_total,
    )

    batch.save()

    return batch
