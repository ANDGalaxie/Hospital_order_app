from collections import defaultdict

from django.db import transaction
from django.db.models import Count, Min, Sum
from django.utils import timezone

from orders.models import Order
from shipments.models import ShipmentBatchItem

from backorders.models import (
    BackorderRootFolder,
    BackorderOrderFolder,
    BackorderLine,
    ExpectedShippingMonthFolder,
    ExpectedShippingProductFolder,
)


def ensure_backorder_root_folders():
    """
    创建待发产品库下的两个根文件夹。
    """
    BackorderRootFolder.objects.update_or_create(
        code=BackorderRootFolder.FolderCode.BACKORDER_ORDERS,
        defaults={
            "name": "待发产品",
            "sort_order": 1,
        },
    )

    BackorderRootFolder.objects.update_or_create(
        code=BackorderRootFolder.FolderCode.EXPECTED_SHIPPING,
        defaults={
            "name": "预计可发产品",
            "sort_order": 2,
        },
    )

    BackorderRootFolder.objects.update_or_create(
        code=BackorderRootFolder.FolderCode.INVENTORY_PRODUCTS,
        defaults={
            "name": "库存产品",
            "sort_order": 3,
        },
    )


def aggregate_requested_by_order(order):
    """
    从 OrderItem 汇总订单需求数量。
    """
    result = {}

    for item in order.items.select_related("product").all().order_by("id"):
        code = item.product_code

        if not code:
            continue

        if code not in result:
            result[code] = {
                "product": item.product,
                "description": item.description or "",
                "requested_quantity": 0,
            }

        result[code]["requested_quantity"] += int(item.requested_quantity or 0)

    return result


def aggregate_shipped_by_order(order):
    """
    从 ShipmentBatchItem 汇总历史已发数量。
    Shipment Batches 是历史事实，所以待发库从这里计算已发。
    """
    result = defaultdict(int)

    qs = (
        ShipmentBatchItem.objects
        .filter(batch__order=order)
        .values("product_code")
        .annotate(total=Sum("shipped_quantity"))
    )

    for row in qs:
        code = row["product_code"]
        result[code] += int(row["total"] or 0)

    return result


def get_existing_manual_fields(order):
    """
    保留人工填写的 expected_shipping_date 和 note。
    """
    result = {}

    for line in BackorderLine.objects.filter(order=order):
        result[line.product_code] = {
            "expected_shipping_date": line.expected_shipping_date,
            "expected_shipping_note": line.expected_shipping_note,
        }

    return result


@transaction.atomic
def sync_backorders_for_order(order):
    """
    重新计算某个订单的当前待发状态。

    自动更新：
      requested_quantity
      shipped_quantity
      remaining_quantity

    保留人工字段：
      expected_shipping_date
      expected_shipping_note
    """
    ensure_backorder_root_folders()

    order_folder, _ = BackorderOrderFolder.objects.get_or_create(
        order=order,
    )

    requested_map = aggregate_requested_by_order(order)
    shipped_map = aggregate_shipped_by_order(order)
    manual_map = get_existing_manual_fields(order)

    now = timezone.now()

    seen_codes = set()

    for code, data in requested_map.items():
        seen_codes.add(code)

        requested_quantity = int(data["requested_quantity"] or 0)
        shipped_quantity = int(shipped_map.get(code, 0))
        remaining_quantity = max(requested_quantity - shipped_quantity, 0)

        manual = manual_map.get(code, {})

        line, created = BackorderLine.objects.get_or_create(
            order=order,
            product_code=code,
            defaults={
                "order_folder": order_folder,
                "product": data["product"],
                "description": data["description"],
            },
        )

        line.order_folder = order_folder
        line.product = data["product"]
        line.description = data["description"]
        line.requested_quantity = requested_quantity
        line.shipped_quantity = shipped_quantity
        line.remaining_quantity = remaining_quantity
        line.last_calculated_at = now

        if created:
            line.expected_shipping_date = manual.get("expected_shipping_date")
            line.expected_shipping_note = manual.get("expected_shipping_note", "")

        line.save()

    # 如果某些旧产品号已经不在订单里了，标记为 completed。
    BackorderLine.objects.filter(order=order).exclude(
        product_code__in=seen_codes
    ).update(
        remaining_quantity=0,
        is_active=False,
        status=BackorderLine.Status.COMPLETED,
        last_calculated_at=now,
    )

    active_lines = BackorderLine.objects.filter(
        order=order,
        is_active=True,
        remaining_quantity__gt=0,
    )

    summary = active_lines.aggregate(
        line_count=Count("id"),
        remaining_total=Sum("remaining_quantity"),
        earliest_date=Min("expected_shipping_date"),
    )

    order_folder.line_count = int(summary["line_count"] or 0)
    order_folder.remaining_total_quantity = int(summary["remaining_total"] or 0)
    order_folder.earliest_expected_shipping_date = summary["earliest_date"]
    order_folder.is_active = order_folder.remaining_total_quantity > 0
    order_folder.last_calculated_at = now
    order_folder.save()

    rebuild_expected_shipping_folders()

    return order_folder


@transaction.atomic
def sync_backorders_for_all_orders():
    """
    重新计算所有已有订单的待发状态。
    """
    ensure_backorder_root_folders()

    orders = (
        Order.objects
        .filter(items__isnull=False)
        .distinct()
        .order_by("bon_de_commande")
    )

    count = 0

    for order in orders:
        sync_backorders_for_order(order)
        count += 1

    rebuild_expected_shipping_folders()

    return count


@transaction.atomic
def rebuild_expected_shipping_folders():
    """
    根据当前 BackorderLine 重建“预计可发产品”的月份和产品汇总。
    """
    now = timezone.now()

    ExpectedShippingProductFolder.objects.all().delete()
    ExpectedShippingMonthFolder.objects.all().delete()

    active_lines = BackorderLine.objects.filter(
        is_active=True,
        remaining_quantity__gt=0,
    )

    month_rows = (
        active_lines
        .values("expected_month_key")
        .annotate(
            line_count=Count("id"),
            product_count=Count("product_code", distinct=True),
            total_remaining_quantity=Sum("remaining_quantity"),
        )
        .order_by("expected_month_key")
    )

    month_folders = {}

    for row in month_rows:
        month_key = row["expected_month_key"] or "no_date"

        if month_key == "no_date":
            display_name = "No expected date"
            sort_order = "9999-99"
        else:
            display_name = month_key
            sort_order = month_key

        month_folder = ExpectedShippingMonthFolder.objects.create(
            month_key=month_key,
            display_name=display_name,
            line_count=int(row["line_count"] or 0),
            product_count=int(row["product_count"] or 0),
            total_remaining_quantity=int(row["total_remaining_quantity"] or 0),
            sort_order=sort_order,
            last_calculated_at=now,
        )

        month_folders[month_key] = month_folder

    product_rows = (
        active_lines
        .values("expected_month_key", "product_code")
        .annotate(
            line_count=Count("id"),
            order_count=Count("order", distinct=True),
            total_remaining_quantity=Sum("remaining_quantity"),
        )
        .order_by("expected_month_key", "product_code")
    )

    for row in product_rows:
        month_key = row["expected_month_key"] or "no_date"
        month_folder = month_folders.get(month_key)

        if not month_folder:
            continue

        sample_line = active_lines.filter(
            expected_month_key=month_key,
            product_code=row["product_code"],
        ).select_related("product").first()

        ExpectedShippingProductFolder.objects.create(
            month_folder=month_folder,
            product=sample_line.product if sample_line else None,
            product_code=row["product_code"],
            line_count=int(row["line_count"] or 0),
            order_count=int(row["order_count"] or 0),
            total_remaining_quantity=int(row["total_remaining_quantity"] or 0),
            last_calculated_at=now,
        )
