import json
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional
from collections import Counter

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import OrderItem
from products.models import Product

from legacy_services.factory_confirmation_extractor import (
    extract_factory_confirmation,
)

from shipments.services.shipment_tracking_service import sync_shipment_batch_from_factory_confirmation
from backorders.services.backorder_sync_service import sync_backorders_for_order


def get_factory_confirmation_workspace(
    confirmation: FactoryConfirmation,
) -> Path:
    """
    为每个工厂确认文件建立独立工作目录。

    以前脚本统一输出到 outputs/factory_confirmation.json。
    在 Django 系统中，每个 FactoryConfirmation 必须有自己的工作目录。
    """
    return (
        Path(settings.MEDIA_ROOT)
        / "order_workspaces"
        / f"order_{confirmation.order.id}"
        / "factory_confirmation"
        / f"confirmation_{confirmation.id}"
    )


def save_factory_confirmation_json(
    confirmation: FactoryConfirmation,
    data: Dict[str, Any],
) -> Path:
    """
    把工厂确认提取结果保存成 JSON，方便调试。

    正式数据保存在：
        confirmation.extracted_confirmation_data
    """
    workspace = get_factory_confirmation_workspace(confirmation)
    workspace.mkdir(parents=True, exist_ok=True)

    json_path = workspace / "factory_confirmation.json"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return json_path


def parse_iso_date(value: Optional[str]):
    """
    把 YYYY-MM-DD 字符串转成 date。
    """
    if not value:
        return None

    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


EXPIRATION_DISCOUNT_RATE = Decimal("0.30")
EXPIRATION_THRESHOLD_DAYS = 365


def calculate_preliminary_discount_rate(expiration_date):
    """
    根据当前日期预估 serial item 是否应该有 30% 折扣。

    注意：
        这是 Admin 中显示用的“预估折扣”。
        最终生成 Factory PO 时，仍然应该用 document_date 重新计算正式折扣。
    """
    if expiration_date is None:
        return Decimal("0.00")

    reference_date = timezone.localdate()
    threshold_date = reference_date + timedelta(days=EXPIRATION_THRESHOLD_DAYS)

    if expiration_date < threshold_date:
        return EXPIRATION_DISCOUNT_RATE

    return Decimal("0.00")


def create_serial_items_from_factory_data(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
) -> int:
    """
    根据 extracted factory confirmation data 创建 SerialItem。

    安全检查：
      1. 同一份文件里重复 serial_number：跳过并 warning
      2. 系统里已经存在的 serial_number：跳过并 warning
      3. 产品号不在原医院订单中：允许记录，但 warning
    """
    confirmation.serial_items.all().delete()

    serial_items = factory_data.get("serial_items", [])
    warnings = factory_data.setdefault("warnings", [])

    order = confirmation.order
    order_product_codes = set(
        order.items.values_list("product_code", flat=True)
    )

    created_count = 0
    seen_serials_in_this_file = set()

    for item in serial_items:
        product_code = str(item.get("product_code") or "").strip()

        if not product_code:
            warnings.append(
                "工厂确认文件中有一行缺少 product_code，已跳过。"
            )
            continue

        product = Product.objects.filter(code=product_code).first()

        serial_number = str(item.get("serial_number") or "").strip()

        if not serial_number:
            warnings.append(
                f"产品 {product_code}: 缺少 serial_number，已跳过。"
            )
            continue

        if serial_number in seen_serials_in_this_file:
            warnings.append(
                f"Serial Number {serial_number} 在当前文件中重复，已跳过重复行。"
            )
            continue

        seen_serials_in_this_file.add(serial_number)

        existing_serial = (
            SerialItem.objects
            .filter(serial_number=serial_number)
            .exclude(factory_confirmation=confirmation)
            .select_related("order", "factory_confirmation")
            .first()
        )

        if existing_serial:
            warnings.append(
                f"Serial Number {serial_number} 已经存在，"
                f"属于 Order {existing_serial.order.bon_de_commande} / "
                f"FactoryConfirmation {existing_serial.factory_confirmation_id}，"
                f"本次已跳过，避免重复计算发货数量。"
            )
            continue

        if product_code not in order_product_codes:
            warnings.append(
                f"产品 {product_code} 出现在工厂确认文件中，"
                f"但不在医院订单 {order.bon_de_commande} 的产品列表里，"
                f"需要人工检查。"
            )

        expiration_date = parse_iso_date(
            item.get("expiration_date_iso")
        )

        discount_rate = calculate_preliminary_discount_rate(expiration_date)

        SerialItem.objects.create(
            factory_confirmation=confirmation,
            order=confirmation.order,
            product=product,
            product_code=product_code,
            serial_number=serial_number,
            expiration_date=expiration_date,

            # Admin 中显示的预估折扣。
            # 最终 Factory PO 生成时仍然要根据 document_date 重新计算。
            discount_rate=discount_rate,

            raw_data=item,
        )

        created_count += 1

    return created_count

def update_order_items_from_factory_data(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
) -> None:
    """
    根据同一个 Order 下所有提取成功的 FactoryConfirmation，
    累计更新 OrderItem：

      confirmed_quantity = 所有成功发货批次中该产品的 SerialItem 数量总和
      backordered_quantity = max(requested_quantity - confirmed_quantity, 0)

    这一步是支持补发货的核心。
    不能再用“最新工厂文件”覆盖 confirmed_quantity。
    """
    order = confirmation.order
    warnings = factory_data.setdefault("warnings", [])

    successful_serial_items = (
        SerialItem.objects
        .filter(
            order=order,
            factory_confirmation__extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
        )
        .select_related("factory_confirmation")
    )

    confirmed_map = Counter()

    for serial in successful_serial_items:
        product_code = str(serial.product_code or "").strip()

        if product_code:
            confirmed_map[product_code] += 1

    order_codes = set(
        order.items.values_list("product_code", flat=True)
    )

    extra_codes = [
        code for code in confirmed_map.keys()
        if code not in order_codes
    ]

    if extra_codes:
        warnings.append(
            "所有成功工厂确认文件中出现了医院订单里没有的产品，需要人工确认："
            + ", ".join(extra_codes)
        )

    for order_item in order.items.all():
        confirmed_quantity = int(
            confirmed_map.get(order_item.product_code, 0)
        )

        requested_quantity = int(order_item.requested_quantity or 0)

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
                f"产品 {order_item.product_code}: 累计已发数量 "
                f"{confirmed_quantity} 大于医院订单数量 {requested_quantity}，"
                f"可能存在超发，需要人工检查。"
            )

        order_item.save(
            update_fields=[
                "confirmed_quantity",
                "backordered_quantity",
                "status",
                "updated_at",
            ]
        )


@transaction.atomic
def extract_factory_confirmation_for_confirmation(
    confirmation: FactoryConfirmation,
) -> Dict[str, Any]:
    """
    为一个 FactoryConfirmation 执行工厂确认文件提取。

    流程：
        1. 读取 confirmation.confirmation_pdf
        2. 调用旧代码 extract_factory_confirmation(pdf_path)
        3. 保存 extracted_confirmation_data
        4. 创建 SerialItem
        5. 更新 OrderItem 的 confirmed / backordered 数量
    """
    if not confirmation.confirmation_pdf:
        raise ValueError("该 FactoryConfirmation 没有上传 confirmation_pdf。")

    pdf_path = Path(confirmation.confirmation_pdf.path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到工厂确认 PDF：{pdf_path}")

    try:
        factory_data = extract_factory_confirmation(pdf_path)

        serial_count = create_serial_items_from_factory_data(
            confirmation=confirmation,
            factory_data=factory_data,
        )

        workspace = get_factory_confirmation_workspace(confirmation)
        json_path = save_factory_confirmation_json(
            confirmation=confirmation,
            data=factory_data,
        )

        factory_data.setdefault("django", {})
        factory_data["django"]["order_id"] = confirmation.order.id
        factory_data["django"]["factory_confirmation_id"] = confirmation.id
        factory_data["django"]["serial_item_count_created"] = serial_count
        factory_data["django"]["workspace"] = str(workspace)
        factory_data["django"]["saved_json_path"] = str(json_path)

        header = factory_data.get("factory_document", {})
        shipping_date = parse_iso_date(
            header.get("shipping_date_only_iso")
        )

        confirmation.extracted_confirmation_data = factory_data
        confirmation.extraction_status = FactoryConfirmation.ExtractionStatus.SUCCESS
        confirmation.extraction_error = ""
        confirmation.extracted_at = timezone.now()
        confirmation.shipping_date = shipping_date

        confirmation.save(
            update_fields=[
                "extracted_confirmation_data",
                "extraction_status",
                "extraction_error",
                "extracted_at",
                "shipping_date",
                "updated_at",
            ]
        )

        # 1. 创建 / 更新 Shipment Batch。
        sync_shipment_batch_from_factory_confirmation(confirmation)

        # 2. 重新累计更新 OrderItem confirmed / backordered 数量。
        update_order_items_from_factory_data(
            confirmation=confirmation,
            factory_data=factory_data,
        )

        # 3. 重新同步当前待发产品库。
        if sync_backorders_for_order:
            sync_backorders_for_order(confirmation.order)

        # 4. 因为 warnings 可能在累计检查时新增，所以再保存一次 extracted data。
        confirmation.extracted_confirmation_data = factory_data
        confirmation.save(
            update_fields=[
                "extracted_confirmation_data",
                "updated_at",
            ]
        )

        return factory_data

    except Exception as exc:
        error_text = traceback.format_exc()

        confirmation.extraction_status = FactoryConfirmation.ExtractionStatus.FAILED
        confirmation.extraction_error = error_text

        confirmation.save(
            update_fields=[
                "extraction_status",
                "extraction_error",
                "updated_at",
            ]
        )

        raise exc
