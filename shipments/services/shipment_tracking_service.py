from collections import Counter
from datetime import date
from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import SerialItem
from shipments.models import (
    ShipmentMonth,
    ShipmentOrderFolder,
    ShipmentBatch,
    ShipmentBatchItem,
)
from shipments.services.shipment_history_service import (
    rebuild_order_shipment_history,
)
try:
    from pricing.services.price_policy_service import get_hospital_order_date
except Exception:
    get_hospital_order_date = None


def get_batch_date(confirmation):
    """
    发货批次日期只能来自 FactoryConfirmation.shipping_date。
    """
    if not confirmation.shipping_date:
        raise ValueError(
            "FactoryConfirmation 缺少 shipping_date，"
            "不能创建 ShipmentBatch；未使用当前日期回退。"
        )

    return confirmation.shipping_date


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


def get_order_folder_date(order):
    """
    Shipment Batches 虚拟文件夹的归属日期。

    规则：
      1. 优先使用医院订单 Date de commande
      2. 如果没有，则使用 order.created_at
      3. 最后才 fallback 到今天

    注意：
      这个日期只用于决定 ShipmentMonth / ShipmentOrderFolder。
      不用于表示实际发货日期。
    """
    if get_hospital_order_date:
        try:
            result = get_hospital_order_date(order)

            if isinstance(result, tuple):
                order_date = result[0]
            else:
                order_date = result

            if order_date:
                return order_date

        except Exception:
            pass

    if getattr(order, "created_at", None):
        return order.created_at.date()

    return timezone.localdate()


def get_or_create_order_shipment_folder(order):
    """
    根据医院最初下单日期，创建 / 获取 ShipmentMonth 和 ShipmentOrderFolder。

    后续补发批次也必须进入同一个 order folder。
    """
    folder_date = get_order_folder_date(order)
    month_key = folder_date.strftime("%Y-%m")

    month, _ = ShipmentMonth.objects.update_or_create(
        month_key=month_key,
        #defaults={
        #    "display_name": month_key,
        #},
    )

    order_folder, _ = ShipmentOrderFolder.objects.update_or_create(
        month=month,
        order=order,
        defaults={},
    )

    return month, order_folder


def validate_explicit_batch_number(confirmation, batch_number):
    """Protect batch identity and require a contiguous explicit sequence."""
    own = ShipmentBatch.objects.filter(factory_confirmation=confirmation).first()
    if own and own.order_id != confirmation.order_id:
        raise ValueError("当前 FactoryConfirmation 已绑定其他医院订单的批次，需要人工检查。")
    if batch_number is None:
        return
    if type(batch_number) is not int or batch_number < 1:
        raise ValueError("batch_number 必须为 >= 1 的整数。")
    if own and own.batch_number != batch_number:
        raise ValueError(
            f"当前 FactoryConfirmation 已经对应 Batch {own.batch_number}，"
            f"但重新提取的文件标记为 Batch {batch_number}，需要人工检查。"
        )
    others = ShipmentBatch.objects.filter(order_id=confirmation.order_id).exclude(
        factory_confirmation=confirmation,
    )
    if others.filter(batch_number=batch_number).exists():
        raise ValueError(
            f"Order {confirmation.order.bon_de_commande} 已经存在 Batch {batch_number}，"
            "请检查是否重复上传。"
        )
    # Iterate existing rows, not a potentially enormous suffix range.
    expected = 1
    for number in others.filter(batch_number__lt=batch_number).order_by(
        "batch_number"
    ).values_list("batch_number", flat=True).distinct():
        if number != expected:
            break
        expected += 1
    if expected < batch_number:
        raise ValueError(
            f"工厂文件明确标记为 Batch {batch_number}，"
            f"但 Order {confirmation.order.bon_de_commande} 尚不存在 Batch {expected}。"
        )


@transaction.atomic
def sync_shipment_batch_from_factory_confirmation(confirmation, explicit_batch_number=None):
    """
    根据一个 FactoryConfirmation 创建或更新发货批次。

    每次重新 extract 工厂确认文件后，可以重新运行这个函数。
    它会删除旧的 shipped/backorder 记录，再重新计算。
    """
    order = confirmation.order
    # Serialize allocation for this order, including legacy auto-number allocation.
    type(order).objects.select_for_update().get(pk=order.pk)
    if explicit_batch_number is None:
        metadata = (confirmation.extracted_confirmation_data or {}).get("django") or {}
        if metadata.get("detected_batch_source") == "factory_order_reference_suffix":
            explicit_batch_number = metadata.get("detected_batch_number")
    validate_explicit_batch_number(confirmation, explicit_batch_number)

    batch_date = get_batch_date(confirmation)
    month_key = batch_date.strftime("%Y-%m")
    month, order_folder = get_or_create_order_shipment_folder(order)

    batch, created = ShipmentBatch.objects.get_or_create(
        factory_confirmation=confirmation,
        defaults={
            "order": order,
            "order_folder": order_folder,
            "batch_number": explicit_batch_number if explicit_batch_number is not None else get_next_batch_number(order),
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

    shipped_this_batch_map = build_shipped_map_from_serial_items(
        order=order,
        confirmation=confirmation,
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
    rebuild_order_shipment_history(order)

    return batch


def rebuild_shipment_order_folders_by_order_date():
    """
    重新按医院订单 Date de commande 归档 ShipmentOrderFolder。

    注意：
    这个函数只整理虚拟文件夹。
    不改变 ShipmentBatch.batch_date。
    """
    orders = set(
        ShipmentBatch.objects
        .select_related("order")
        .values_list("order_id", flat=True)
    )

    for order_id in orders:
        if not order_id:
            continue

        from orders.models import Order

        order = Order.objects.filter(id=order_id).first()

        if not order:
            continue

        get_or_create_order_shipment_folder(order)

def rebuild_shipment_order_folders_by_order_date():
    from orders.models import Order

    order_ids = (
        ShipmentBatch.objects
        .values_list("order_id", flat=True)
        .distinct()
    )

    for order_id in order_ids:
        if not order_id:
            continue

        order = Order.objects.filter(id=order_id).first()

        if not order:
            continue

        month, order_folder = get_or_create_order_shipment_folder(order)

        update_fields = []

        for batch in ShipmentBatch.objects.filter(order=order):
            if hasattr(batch, "order_folder"):
                batch.order_folder = order_folder
                update_fields.append("order_folder")

            if hasattr(batch, "month"):
                batch.month = month
                update_fields.append("month")

            if update_fields:
                batch.save(update_fields=list(set(update_fields)))
