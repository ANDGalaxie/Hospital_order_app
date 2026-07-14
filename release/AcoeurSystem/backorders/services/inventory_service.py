import json
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from legacy_services.factory_confirmation_extractor import extract_factory_confirmation
from products.models import Product
from orders.models import Order
from backorders.models import (
    BackorderLine,
    InventoryAllocation,
    InventoryBatch,
    InventoryItem,
    InventoryProductFolder,
)

try:
    from pricing.services.price_policy_service import get_hospital_order_date
except Exception:
    get_hospital_order_date = None


def parse_iso_date(value):
    if not value:
        return None

    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def get_order_date_for_display(order):
    """
    可分配订单里显示的下单日期。
    优先使用医院订单 Date de commande。
    """
    if get_hospital_order_date:
        try:
            order_date, source = get_hospital_order_date(order)
            if order_date:
                return order_date
        except Exception:
            pass

    if getattr(order, "created_at", None):
        return order.created_at.date()

    return None


def format_date(value):
    if not value:
        return "未知日期"

    return value.strftime("%Y-%m-%d")


@transaction.atomic
def rebuild_inventory_product_folders():
    """
    根据 InventoryItem 重建库存产品汇总。

    注意：
    这里不要直接用 values_list(...).distinct()，
    因为 InventoryItem 有默认 ordering，可能导致同一个 product_code 被重复返回。
    所以这里用 Python set 做最终去重。
    """
    InventoryProductFolder.objects.all().delete()

    product_codes = sorted(
        {
            str(code).strip()
            for code in InventoryItem.objects.values_list("product_code", flat=True)
            if code and str(code).strip()
        }
    )

    now = timezone.now()

    for product_code in product_codes:
        items = InventoryItem.objects.filter(product_code=product_code)

        total_quantity = items.count()
        available_quantity = items.filter(status=InventoryItem.Status.AVAILABLE).count()
        reserved_quantity = items.filter(status=InventoryItem.Status.RESERVED).count()
        allocated_quantity = items.filter(status=InventoryItem.Status.ALLOCATED).count()
        cancelled_quantity = items.filter(status=InventoryItem.Status.CANCELLED).count()

        earliest = (
            items
            .filter(
                status=InventoryItem.Status.AVAILABLE,
                expiration_date__isnull=False,
            )
            .aggregate(v=Min("expiration_date"))
            .get("v")
        )

        product = (
            Product.objects
            .filter(code=product_code)
            .first()
        )

        InventoryProductFolder.objects.update_or_create(
            product_code=product_code,
            defaults={
                "product": product,
                "total_quantity": total_quantity,
                "available_quantity": available_quantity,
                "reserved_quantity": reserved_quantity,
                "allocated_quantity": allocated_quantity,
                "cancelled_quantity": cancelled_quantity,
                "earliest_expiration_date": earliest,
                "last_calculated_at": now,
            },
        )


def get_allocatable_orders_for_product(product_code: str) -> List[Dict[str, Any]]:
    """
    查询某个产品号可以分配给哪些订单。

    排序规则：
      1. 医院下单日期从早到晚
      2. bon_de_commande 从小到大
    """
    lines = (
        BackorderLine.objects
        .filter(
            product_code=product_code,
            is_active=True,
            remaining_quantity__gt=0,
        )
        .select_related("order", "order__hospital")
    )

    result = []

    for line in lines:
        order = line.order
        order_date = get_order_date_for_display(order)

        hospital_name = "-"
        if getattr(order, "hospital", None):
            hospital_name = order.hospital.name

        result.append(
            {
                "order_id": order.id,
                "bon_de_commande": order.bon_de_commande,
                "order_date": order_date,
                "order_date_display": format_date(order_date),
                "hospital_name": hospital_name,
                "remaining_quantity": line.remaining_quantity,
                "expected_shipping_date": line.expected_shipping_date,
                "expected_shipping_date_display": format_date(
                    line.expected_shipping_date
                ),
            }
        )

    result.sort(
        key=lambda x: (
            x["order_date"] or datetime(2999, 12, 31).date(),
            str(x["bon_de_commande"]),
        )
    )

    return result


@transaction.atomic
def extract_inventory_batch(batch: InventoryBatch) -> Dict[str, Any]:
    """
    提取一个库存批次 PDF，生成 InventoryItem。

    这一步只入库，不分配订单，不生成 ShipmentBatch。
    """
    if not batch.source_pdf:
        raise ValueError("该库存批次没有上传 source_pdf。")

    pdf_path = Path(batch.source_pdf.path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到库存批次 PDF：{pdf_path}")

    # 第一版安全规则：
    # 如果一个 batch 已经有已分配库存，不允许重新提取，避免破坏历史。
    if batch.items.filter(status=InventoryItem.Status.ALLOCATED).exists():
        raise ValueError(
            "该库存批次中已经有已分配的 Serial，不能重新提取。"
        )

    try:
        data = extract_factory_confirmation(pdf_path)

        warnings = data.setdefault("warnings", [])

        batch.items.all().delete()

        serial_items = data.get("serial_items", [])
        created_count = 0
        skipped_count = 0
        seen_in_file = set()

        for item in serial_items:
            product_code = str(item.get("product_code") or "").strip()
            serial_number = str(item.get("serial_number") or "").strip()

            if not product_code:
                warnings.append("有一行缺少 product_code，已跳过。")
                skipped_count += 1
                continue

            if not serial_number:
                warnings.append(f"产品 {product_code}: 缺少 serial_number，已跳过。")
                skipped_count += 1
                continue

            if serial_number in seen_in_file:
                warnings.append(
                    f"Serial Number {serial_number} 在当前文件中重复，已跳过。"
                )
                skipped_count += 1
                continue

            seen_in_file.add(serial_number)

            if (
                InventoryItem.objects
                .filter(serial_number=serial_number)
                .exclude(batch=batch)
                .exists()
            ):
                warnings.append(
                    f"Serial Number {serial_number} 已经存在于库存系统中，已跳过。"
                )
                skipped_count += 1
                continue

            # 也检查是否已经作为历史发货 SerialItem 出现过。
            try:
                from factory_confirmations.models import SerialItem

                if SerialItem.objects.filter(serial_number=serial_number).exists():
                    warnings.append(
                        f"Serial Number {serial_number} 已经存在于历史发货记录中，已跳过。"
                    )
                    skipped_count += 1
                    continue
            except Exception:
                pass

            product = Product.objects.filter(code=product_code).first()

            expiration_date = parse_iso_date(
                item.get("expiration_date_iso")
            )

            InventoryItem.objects.create(
                batch=batch,
                product=product,
                product_code=product_code,
                serial_number=serial_number,
                expiration_date=expiration_date,
                status=InventoryItem.Status.AVAILABLE,
                raw_data=item,
            )

            created_count += 1

        data.setdefault("django", {})
        data["django"]["inventory_batch_id"] = batch.id
        data["django"]["inventory_item_count_created"] = created_count
        data["django"]["inventory_item_count_skipped"] = skipped_count

        header = data.get("factory_document", {}) or {}
        shipping_date = parse_iso_date(
            header.get("shipping_date_only_iso")
        )

        if shipping_date and not batch.batch_date:
            batch.batch_date = shipping_date

        batch.extracted_data = data
        batch.extraction_status = InventoryBatch.ExtractionStatus.SUCCESS
        batch.extraction_error = ""
        batch.extracted_at = timezone.now()

        batch.save(
            update_fields=[
                "batch_date",
                "extracted_data",
                "extraction_status",
                "extraction_error",
                "extracted_at",
                "updated_at",
            ]
        )

        rebuild_inventory_product_folders()

        return data

    except Exception:
        error_text = traceback.format_exc()

        batch.extraction_status = InventoryBatch.ExtractionStatus.FAILED
        batch.extraction_error = error_text

        batch.save(
            update_fields=[
                "extraction_status",
                "extraction_error",
                "updated_at",
            ]
        )

        raise

@transaction.atomic
def allocate_inventory_to_order(
    product_code: str,
    order: Order,
    quantity: int,
    created_by=None,
    notes: str = "",
) -> InventoryAllocation:
    """
    将某个产品的库存 Serial Number 预留给一个订单。

    安全规则：
      1. 只能分配给仍有待发数量的订单
      2. 预留数量不能超过待发数量
      3. 可用库存必须足够
      4. 自动选择有效期最早的 serial number
      5. 这里只预留，不生成 ShipmentBatch
    """
    product_code = str(product_code or "").strip()

    if not product_code:
        raise ValueError("缺少 product_code。")

    try:
        quantity = int(quantity)
    except Exception:
        raise ValueError("预留数量必须是整数。")

    if quantity <= 0:
        raise ValueError("预留数量必须大于 0。")

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

    if quantity > remaining_quantity:
        raise ValueError(
            f"预留数量 {quantity} 超过订单待发数量 {remaining_quantity}。"
        )

    available_items = list(
        InventoryItem.objects
        .select_for_update()
        .filter(
            product_code=product_code,
            status=InventoryItem.Status.AVAILABLE,
        )
        .order_by("expiration_date", "serial_number")[:quantity]
    )

    if len(available_items) < quantity:
        raise ValueError(
            f"库存不足：产品 {product_code} 需要预留 {quantity}，"
            f"但当前可用库存只有 {len(available_items)}。"
        )

    product = Product.objects.filter(code=product_code).first()

    allocation = InventoryAllocation.objects.create(
        order=order,
        product=product,
        product_code=product_code,
        quantity_requested=quantity,
        allocated_count=len(available_items),
        status=InventoryAllocation.Status.RESERVED,
        notes=notes or "",
        created_by=created_by,
    )

    now = timezone.now()

    for item in available_items:
        item.status = InventoryItem.Status.RESERVED
        item.allocated_order = order
        item.allocation = allocation
        item.allocated_at = now
        item.save(
            update_fields=[
                "status",
                "allocated_order",
                "allocation",
                "allocated_at",
            ]
        )

    rebuild_inventory_product_folders()

    return allocation


@transaction.atomic
def cancel_inventory_allocation(
    allocation: InventoryAllocation,
) -> InventoryAllocation:
    """
    取消一个库存预留记录。

    只有“已预留”状态可以取消。
    已经生成 ShipmentBatch 的记录不能取消。
    """
    if allocation.status != InventoryAllocation.Status.RESERVED:
        raise ValueError(
            "只有“已预留”的库存记录可以取消。"
        )

    items = list(
        allocation.items.select_for_update().all()
    )

    for item in items:
        item.status = InventoryItem.Status.AVAILABLE
        item.allocated_order = None
        item.allocation = None
        item.allocated_at = None
        item.save(
            update_fields=[
                "status",
                "allocated_order",
                "allocation",
                "allocated_at",
            ]
        )

    allocation.status = InventoryAllocation.Status.CANCELLED
    allocation.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    rebuild_inventory_product_folders()

    return allocation