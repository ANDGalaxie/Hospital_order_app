import json
import re
import traceback
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from products.models import Product
from shipments.models import ShipmentBatch, ShipmentBatchItem
from shipments.services.shipment_tracking_service import (
    sync_shipment_batch_from_factory_confirmation,
)

from legacy_services.factory_confirmation_extractor import (
    extract_factory_confirmation,
)

# Backorder 同步不是工厂采购文件提取的核心能力。
# 如果 backorders app 暂时不可用，不应该导致整个 service 无法 import。
try:
    from backorders.services.backorder_sync_service import sync_backorders_for_order
except Exception:
    sync_backorders_for_order = None


# Workflow 同步和验证是上传工厂采购文件后的正常后续动作。
# 这里仍然用 try，是为了避免循环 import 或局部开发环境缺少 workflow service 时导致系统完全启动失败。
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


# ============================================================
# 1. 通用工具函数
# ============================================================

def json_safe(value: Any) -> Any:
    """
    把对象转换成 JSONField / json.dump 可以安全保存的结构。

    用途：
      - workflow validation 结果里可能包含 datetime、Decimal 等对象。
      - 这些对象直接写入 JSONField 或 json.dump 可能报错。
    """
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def get_model_instance_from_sync_result(sync_result: Any, required_attrs=None):
    """
    有些同步函数可能返回：
        instance
    也可能返回：
        (instance, created)
        (instance, status)

    这里统一从返回值里取出真正的 model instance。

    required_attrs:
      - 用于进一步确认这个对象是不是我们要的类型。
      - 例如 workflow item 通常有 workflow_status / validation_status。
    """
    if sync_result is None:
        return None

    if required_attrs is None:
        required_attrs = []

    def looks_like_target(value: Any) -> bool:
        if not hasattr(value, "id"):
            return False

        for attr in required_attrs:
            if not hasattr(value, attr):
                return False

        return True

    if isinstance(sync_result, tuple):
        for value in sync_result:
            if looks_like_target(value):
                return value

        return None

    if looks_like_target(sync_result):
        return sync_result

    return None


def parse_iso_date(value: Optional[str]):
    """
    把 YYYY-MM-DD 字符串转成 date。

    legacy extractor 输出的字段通常是：
        shipping_date_only_iso
        expiration_date_iso
    """
    if not value:
        return None

    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_order_number(value: Any) -> str:
    """
    把订单号统一成纯数字，方便匹配。

    示例：
        "150 222" -> "150222"
        "BON DE COMMANDE N° 150222" -> "150222"
    """
    return re.sub(r"\D", "", str(value or ""))


def collect_text_from_any(value: Any) -> str:
    """
    从 dict / list / str 里递归收集文本。

    用途：
      - legacy extractor 不一定总是把 bon_de_commande 放在固定字段。
      - 所以需要从整个 extracted data 里兜底搜索。
    """
    texts = []

    if isinstance(value, str):
        texts.append(value)

    elif isinstance(value, dict):
        for child_value in value.values():
            child_text = collect_text_from_any(child_value)
            if child_text:
                texts.append(child_text)

    elif isinstance(value, list):
        for item in value:
            child_text = collect_text_from_any(item)
            if child_text:
                texts.append(child_text)

    return " ".join(texts)


# ============================================================
# 2. 工作目录与调试 JSON
# ============================================================

def get_factory_confirmation_workspace(
    confirmation: FactoryConfirmation,
) -> Path:
    """
    为每个工厂采购文件建立独立工作目录。

    已匹配订单：
        media/order_workspaces/order_x/factory_confirmation/confirmation_y

    未匹配订单：
        media/factory_confirmation_workspaces/unmatched/confirmation_y
    """
    if confirmation.order_id:
        return (
            Path(settings.MEDIA_ROOT)
            / "order_workspaces"
            / f"order_{confirmation.order_id}"
            / "factory_confirmation"
            / f"confirmation_{confirmation.id}"
        )

    return (
        Path(settings.MEDIA_ROOT)
        / "factory_confirmation_workspaces"
        / "unmatched"
        / f"confirmation_{confirmation.id}"
    )


def save_factory_confirmation_json(
    confirmation: FactoryConfirmation,
    data: Dict[str, Any],
) -> Path:
    """
    保存一份调试 JSON。

    正式数据仍然保存在：
        confirmation.extracted_confirmation_data
    """
    workspace = get_factory_confirmation_workspace(confirmation)
    workspace.mkdir(parents=True, exist_ok=True)

    json_path = workspace / "factory_confirmation.json"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    return json_path


def get_confirmation_extracted_data(
    confirmation: FactoryConfirmation,
) -> Dict[str, Any]:
    data = confirmation.extracted_confirmation_data or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    if not isinstance(data, dict):
        data = {}

    data.setdefault("warnings", [])
    data.setdefault("django", {})

    return data


def stamp_confirmation_debug_metadata(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
    *,
    workspace: Optional[Path] = None,
    json_path: Optional[Path] = None,
) -> Dict[str, Any]:
    django_data = factory_data.setdefault("django", {})
    django_data["factory_confirmation_id"] = confirmation.id

    if confirmation.order_id:
        django_data["selected_order_id"] = confirmation.order_id
        django_data["order_id"] = confirmation.order_id
        django_data["order_bon_de_commande"] = confirmation.order.bon_de_commande
        django_data["order_match_source"] = "selected_order"
    else:
        django_data.setdefault("order_match_source", "auto_match")

    django_data["manual_confirmation"] = bool(
        confirmation.bon_de_commande_manual_confirmed
    )
    django_data["manual_note"] = (
        confirmation.bon_de_commande_manual_note or ""
    )

    if workspace is not None:
        django_data["workspace"] = str(workspace)

    if json_path is not None:
        django_data["saved_json_path"] = str(json_path)

    return django_data


def save_confirmation_extraction_state(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
    *,
    shipping_date=None,
    extraction_status=None,
    extraction_error="",
) -> Dict[str, Any]:
    workspace = get_factory_confirmation_workspace(confirmation)
    json_path = save_factory_confirmation_json(
        confirmation=confirmation,
        data=factory_data,
    )

    stamp_confirmation_debug_metadata(
        confirmation,
        factory_data,
        workspace=workspace,
        json_path=json_path,
    )

    confirmation.extracted_confirmation_data = json_safe(factory_data)
    confirmation.extraction_status = (
        extraction_status
        or FactoryConfirmation.ExtractionStatus.SUCCESS
    )
    confirmation.extraction_error = extraction_error
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

    return factory_data


# ============================================================
# 3. bon de commande 提取与订单匹配
# ============================================================

def extract_bon_de_commande_from_text(text: str) -> Optional[str]:
    """
    从文本中宽松提取 bon de commande。

    支持格式：
        BON DE COMMANDE N° 150222
        BON DE COMMANDE N° 150 222
        Order: BON DE COMMANDE N° 150222
        Order 150222
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()

    patterns = [
        r"\bBON\s+DE\s+COMMANDE\s*(?:N|N°|Nº|NO|N0|NUMERO|NUMÉRO)?\s*[°º:]?\s*([0-9][0-9\s]{2,20})",
        r"\bORDER\s*:?\s*BON\s+DE\s+COMMANDE\s*(?:N|N°|Nº|NO|N0)?\s*[°º:]?\s*([0-9][0-9\s]{2,20})",
        r"\bORDER\s*:?\s*([0-9][0-9\s]{2,20})",
    ]

    for pattern in patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)

        if not match:
            continue

        digits = normalize_order_number(match.group(1))

        if 3 <= len(digits) <= 12:
            return digits

    return None


def extract_bon_de_commande_from_factory_data(
    factory_data: Dict[str, Any],
) -> Optional[str]:
    """
    优先从 legacy extractor 的固定字段读取 bon de commande。
    如果固定字段没有，再从整个 factory_data 里递归搜索。
    """
    candidates = []

    factory_document = factory_data.get("factory_document") or {}
    summary = factory_data.get("summary") or {}
    django_data = factory_data.get("django") or {}

    for source in [factory_document, summary, django_data, factory_data]:
        if not isinstance(source, dict):
            continue

        for key in [
            "bon_de_commande",
            "order_number",
            "customer_order_number",
            "customer_reference",
            "order",
        ]:
            value = source.get(key)

            # 避免把 dict/list 直接转字符串后误提取大量数字。
            if isinstance(value, (str, int, float)):
                candidates.append(value)

    for value in candidates:
        digits = normalize_order_number(value)
        if 3 <= len(digits) <= 12:
            return digits

    full_text = collect_text_from_any(factory_data)
    return extract_bon_de_commande_from_text(full_text)


def extract_bon_de_commande_from_pdf_text(pdf_path: Path) -> Optional[str]:
    """
    如果 factory_data 里没有找到 bon de commande，则尝试直接读取 PDF 文本层。

    使用 PyMuPDF / fitz。
    如果环境没有 fitz，安静跳过，不影响 OCR 结果。
    """
    try:
        import fitz
    except Exception:
        return None

    try:
        text_parts = []

        with fitz.open(str(pdf_path)) as document:
            for page in document:
                text_parts.append(page.get_text("text"))

        return extract_bon_de_commande_from_text("\n".join(text_parts))

    except Exception:
        return None


def find_order_by_bon_de_commande(value: Any) -> Optional[Order]:
    """
    用归一化后的订单号匹配 Order。

    这样可以避免：
        数据库：150222
        PDF：150 222
    这类格式差异导致匹配失败。
    """
    target = normalize_order_number(value)

    if not target:
        return None

    orders = (
        Order.objects
        .exclude(bon_de_commande="")
        .only("id", "bon_de_commande")
    )

    for order in orders:
        if normalize_order_number(order.bon_de_commande) == target:
            return order

    return None


def attach_order_from_factory_confirmation_data(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
    pdf_path: Path,
) -> Optional[Order]:
    """
    给 FactoryConfirmation 自动绑定 Order。

    逻辑：
      1. 如果 confirmation 已经有人选了 order，保留它。
      2. 否则从 factory_data / PDF 文本里提取 bon de commande。
      3. 用归一化订单号匹配 Order。
      4. 匹配成功后保存 confirmation.order。
    """
    warnings = factory_data.setdefault("warnings", [])
    django_data = factory_data.setdefault("django", {})

    detected_bon = extract_bon_de_commande_from_factory_data(factory_data)

    if not detected_bon:
        detected_bon = extract_bon_de_commande_from_pdf_text(pdf_path)

    django_data["detected_bon_de_commande"] = detected_bon
    django_data["detected_bon_de_commande_normalized"] = normalize_order_number(
        detected_bon
    )

    # 情况 A：用户或 Admin 已经手动选择了订单。
    if confirmation.order_id:
        selected_order = confirmation.order

        if detected_bon:
            selected_normalized = normalize_order_number(
                selected_order.bon_de_commande
            )
            detected_normalized = normalize_order_number(detected_bon)

            if (
                selected_normalized
                and detected_normalized
                and selected_normalized != detected_normalized
            ):
                warnings.append(
                    f"工厂采购文件中识别到 bon de commande {detected_bon}，"
                    f"但当前选择的医院订单是 {selected_order.bon_de_commande}，请人工确认。"
                )
                django_data["order_match_status"] = (
                    "selected_order_mismatch_detected_bon"
                )
            else:
                django_data["order_match_status"] = (
                    "selected_order_confirmed_by_detected_bon"
                )
        else:
            django_data["order_match_status"] = "selected_order_without_detected_bon"

        django_data["order_id"] = selected_order.id
        django_data["order_bon_de_commande"] = selected_order.bon_de_commande

        return selected_order

    # 情况 B：自动匹配订单。
    matched_order = find_order_by_bon_de_commande(detected_bon)

    if matched_order:
        confirmation.order = matched_order
        confirmation.save(update_fields=["order", "updated_at"])

        django_data["order_match_status"] = "matched_by_detected_bon_de_commande"
        django_data["order_id"] = matched_order.id
        django_data["order_bon_de_commande"] = matched_order.bon_de_commande

        return matched_order

    # 情况 C：没有匹配到订单。
    django_data["order_match_status"] = "not_matched"

    if detected_bon:
        warnings.append(
            f"已从工厂采购文件中识别到 bon de commande {detected_bon}，"
            f"但系统中没有找到对应医院订单。"
        )
    else:
        warnings.append(
            "未能从工厂采购文件中识别 bon de commande，无法自动匹配医院订单。"
        )

    return None


# ============================================================
# 4. 批次顺序、折扣、SerialItem 创建
# ============================================================

EXPIRATION_DISCOUNT_RATE = Decimal("0.30")
EXPIRATION_THRESHOLD_DAYS = 365


def validate_confirmation_batch_sequence(
    confirmation: FactoryConfirmation,
) -> None:
    """
    保护 FactoryConfirmation 和 ShipmentBatch 的顺序关系。

    规则：
      1. 一个订单的第一份发货文件应该是 initial。
      2. 已经有其他批次后，新文件不应该再是 initial。
      3. 同一个 confirmation 重新 extract 时，不重复创建新批次。
    """
    if not confirmation.order_id:
        raise ValueError("工厂采购文件还没有匹配医院订单，不能检查批次顺序。")

    order = confirmation.order

    own_batch_exists = ShipmentBatch.objects.filter(
        factory_confirmation=confirmation,
    ).exists()

    other_batches_exist = (
        ShipmentBatch.objects
        .filter(order=order)
        .exclude(factory_confirmation=confirmation)
        .exists()
    )

    confirmation_type = getattr(
        confirmation,
        "confirmation_type",
        FactoryConfirmation.ConfirmationType.INITIAL,
    )

    if not own_batch_exists and not other_batches_exist:
        if confirmation_type == FactoryConfirmation.ConfirmationType.REPLENISHMENT:
            raise ValueError(
                f"Order {order.bon_de_commande} 还没有任何发货批次，"
                "第一份工厂采购文件不能选择“补发货”。请改成“首批发货”。"
            )

    if not own_batch_exists and other_batches_exist:
        if confirmation_type == FactoryConfirmation.ConfirmationType.INITIAL:
            raise ValueError(
                f"Order {order.bon_de_commande} 已经存在发货批次，"
                "新的工厂采购文件不能再选择“首批发货”。请改成“补发货”。"
            )


def calculate_preliminary_discount_rate(expiration_date):
    """
    根据当前日期预估 serial item 是否应该有 30% 折扣。

    注意：
      这里只是 Admin / Portal 展示用的预估折扣。
      最终生成 Factory PO 时，仍然应该根据 document_date 重新计算正式折扣。
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
      1. 同一份文件里重复 serial_number：跳过并 warning。
      2. 系统里已经存在的 serial_number：跳过并 warning。
      3. 产品号不在原医院订单中：允许记录，但 warning。
    """
    if not confirmation.order_id:
        raise ValueError("工厂采购文件还没有匹配医院订单，不能创建 SerialItem。")

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
            warnings.append("工厂采购文件中有一行缺少 product_code，已跳过。")
            continue

        product = Product.objects.filter(code=product_code).first()

        serial_number = str(item.get("serial_number") or "").strip()

        if not serial_number:
            warnings.append(f"产品 {product_code}: 缺少 serial_number，已跳过。")
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
            existing_order_number = (
                existing_serial.order.bon_de_commande
                if existing_serial.order_id and existing_serial.order
                else "未知订单"
            )

            warnings.append(
                f"Serial Number {serial_number} 已经存在，"
                f"属于 Order {existing_order_number} / "
                f"FactoryConfirmation {existing_serial.factory_confirmation_id}，"
                "本次已跳过，避免重复计算发货数量。"
            )
            continue

        if product_code not in order_product_codes:
            warnings.append(
                f"产品 {product_code} 出现在工厂采购文件中，"
                f"但不在医院订单 {order.bon_de_commande} 的产品列表里，"
                "需要人工检查。"
            )

        expiration_date = parse_iso_date(
            item.get("expiration_date_iso")
        )

        discount_rate = calculate_preliminary_discount_rate(expiration_date)

        SerialItem.objects.create(
            factory_confirmation=confirmation,
            order=order,
            product=product,
            product_code=product_code,
            serial_number=serial_number,
            expiration_date=expiration_date,
            discount_rate=discount_rate,
            raw_data=item,
        )

        created_count += 1

    return created_count


# ============================================================
# 5. OrderItem confirmed / backordered 重新计算
# ============================================================

def recalculate_order_items_from_shipment_batches(
    order: Order,
    warnings=None,
) -> None:
    """
    从 ShipmentBatchItem 累计计算 OrderItem 的 confirmed / backordered。

    注意：
      OrderItem.confirmed_quantity 是累计已发数量。
      ShipmentBatchItem.shipped_quantity 是当前批次数量。
    """
    if warnings is None:
        warnings = []

    shipped_map = Counter()

    batch_items = ShipmentBatchItem.objects.filter(
        batch__order=order,
    )

    for batch_item in batch_items:
        product_code = str(batch_item.product_code or "").strip()

        if not product_code:
            continue

        shipped_quantity = int(
            getattr(batch_item, "shipped_quantity", 0) or 0
        )

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
            "发货批次中出现了医院订单里没有的产品，需要人工确认："
            + ", ".join(extra_codes)
        )

    for order_item in order.items.all():
        product_code = str(order_item.product_code or "").strip()
        requested_quantity = int(order_item.requested_quantity or 0)
        confirmed_quantity = int(shipped_map.get(product_code, 0))

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


# ============================================================
# 6. ShipmentBatch / Workflow 同步
# ============================================================

def sync_confirmation_to_shipment_and_workflow(
    confirmation: FactoryConfirmation,
    factory_data: Dict[str, Any],
):
    """
    把提取成功的 FactoryConfirmation 同步到：
      1. ShipmentBatch
      2. OrderItem confirmed/backordered
      3. Backorder
      4. DocumentWorkflowItem
      5. Workflow validation

    返回：
        shipment_batch, workflow_item, workflow_validation_result
    """
    warnings = factory_data.setdefault("warnings", [])

    # 1. FactoryConfirmation -> ShipmentBatch。
    shipment_sync_result = sync_shipment_batch_from_factory_confirmation(
        confirmation
    )

    shipment_batch = get_model_instance_from_sync_result(
        shipment_sync_result,
        required_attrs=["order_id"],
    )

    if shipment_batch is None:
        raise ValueError(
            "ShipmentBatch 同步失败："
            "sync_shipment_batch_from_factory_confirmation 没有返回 ShipmentBatch。"
        )

    # 2. 从所有 ShipmentBatchItem 累计更新 OrderItem。
    recalculate_order_items_from_shipment_batches(
        order=confirmation.order,
        warnings=warnings,
    )

    # 3. 同步当前待发产品库。
    if sync_backorders_for_order:
        sync_backorders_for_order(confirmation.order)

    # 4. ShipmentBatch -> DocumentWorkflowItem。
    workflow_item = None
    workflow_validation_result = None

    if sync_document_workflow_item_for_batch:
        workflow_sync_result = sync_document_workflow_item_for_batch(
            shipment_batch
        )

        workflow_item = get_model_instance_from_sync_result(
            workflow_sync_result,
            required_attrs=["workflow_status", "validation_status"],
        )

    if workflow_item and validate_document_workflow_item:
        workflow_validation_result = validate_document_workflow_item(
            workflow_item,
            save=True,
        )

        workflow_item.refresh_from_db()

    elif not workflow_item:
        warnings.append(
            "ShipmentBatch 已创建，但没有成功创建或取得 DocumentWorkflowItem。"
        )

    return shipment_batch, workflow_item, workflow_validation_result


@transaction.atomic
def finalize_factory_confirmation_after_order_match(
    confirmation: FactoryConfirmation,
    user=None,
):
    """
    基于已有 extracted_confirmation_data 继续执行后处理。

    用于：
      1. 预选订单上传后，OCR 已完成但需要人工确认；
      2. 未自动匹配时，人工补选订单后继续；
      3. 重复执行时尽量保持幂等。
    """
    if not confirmation.order_id:
        raise ValueError("FactoryConfirmation 还没有关联医院订单。")

    factory_data = get_confirmation_extracted_data(
        confirmation
    )

    header = factory_data.get("factory_document", {}) or {}
    shipping_date = parse_iso_date(
        header.get("shipping_date_only_iso")
    )

    validate_confirmation_batch_sequence(
        confirmation
    )

    serial_count = create_serial_items_from_factory_data(
        confirmation=confirmation,
        factory_data=factory_data,
    )

    django_data = stamp_confirmation_debug_metadata(
        confirmation,
        factory_data,
    )
    django_data["serial_item_count_created"] = serial_count
    django_data["finalized_after_order_match"] = True
    django_data["finalized_by_user_id"] = getattr(
        user,
        "id",
        None,
    )

    save_confirmation_extraction_state(
        confirmation=confirmation,
        factory_data=factory_data,
        shipping_date=shipping_date,
        extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
        extraction_error="",
    )

    shipment_batch, workflow_item, workflow_validation_result = (
        sync_confirmation_to_shipment_and_workflow(
            confirmation=confirmation,
            factory_data=factory_data,
        )
    )

    django_data["shipment_batch_id"] = shipment_batch.id
    django_data["workflow_item_id"] = workflow_item.id if workflow_item else None
    django_data["workflow_validation_status"] = (
        workflow_item.validation_status if workflow_item else None
    )
    django_data["workflow_status"] = (
        workflow_item.workflow_status if workflow_item else None
    )
    django_data["workflow_validation_result"] = json_safe(
        workflow_item.validation_data
        if workflow_item and workflow_item.validation_data
        else workflow_validation_result
    )

    save_confirmation_extraction_state(
        confirmation=confirmation,
        factory_data=factory_data,
        shipping_date=shipping_date,
        extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
        extraction_error="",
    )

    return shipment_batch, workflow_item, workflow_validation_result


# ============================================================
# 7. 主入口：提取工厂采购文件
# ============================================================

@transaction.atomic
def extract_factory_confirmation_for_confirmation(
    confirmation: FactoryConfirmation,
) -> Dict[str, Any]:
    """
    为一个 FactoryConfirmation 执行工厂采购文件提取。

    完整流程：
      1. 读取 confirmation.confirmation_pdf
      2. 调用旧代码 extract_factory_confirmation(pdf_path)
      3. 自动识别 bon de commande 并匹配 Order
      4. 创建 SerialItem
      5. 同步 ShipmentBatch
      6. 重新计算 OrderItem confirmed/backordered
      7. 同步 Backorder
      8. 自动创建 DocumentWorkflowItem
      9. 自动执行 Workflow 验证
      10. 保存 extracted_confirmation_data
    """
    if not confirmation.confirmation_pdf:
        raise ValueError("该 FactoryConfirmation 没有上传 confirmation_pdf。")

    pdf_path = Path(confirmation.confirmation_pdf.path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到工厂采购 PDF：{pdf_path}")

    try:
        factory_data = extract_factory_confirmation(pdf_path)
        factory_data.setdefault("warnings", [])
        factory_data.setdefault("django", {})

        # --------------------------------------------------------
        # 1. 自动匹配医院订单
        # --------------------------------------------------------
        had_preselected_order = bool(confirmation.order_id)
        matched_order = attach_order_from_factory_confirmation_data(
            confirmation=confirmation,
            factory_data=factory_data,
            pdf_path=pdf_path,
        )

        header = factory_data.get("factory_document", {})
        shipping_date = parse_iso_date(
            header.get("shipping_date_only_iso")
        )

        django_data = stamp_confirmation_debug_metadata(
            confirmation,
            factory_data,
        )
        detected_bon = django_data.get("detected_bon_de_commande")

        # 没有匹配到订单时：保存提取结果，但不继续创建 Serial / Shipment / Workflow。
        if matched_order is None:
            save_confirmation_extraction_state(
                confirmation=confirmation,
                factory_data=factory_data,
                shipping_date=shipping_date,
                extraction_status=FactoryConfirmation.ExtractionStatus.FAILED,
                extraction_error=(
                "工厂采购文件已提取，但没有自动匹配到医院订单。"
                "请在详情页或 Admin 中人工确认关联订单后重新提取。"
                ),
            )
            return factory_data

        selected_normalized = normalize_order_number(
            matched_order.bon_de_commande
        )
        detected_normalized = normalize_order_number(
            detected_bon
        )

        if (
            had_preselected_order
            and detected_normalized
            and selected_normalized
            and detected_normalized != selected_normalized
            and not confirmation.bon_de_commande_manual_confirmed
        ):
            django_data["requires_manual_confirmation"] = True
            save_confirmation_extraction_state(
                confirmation=confirmation,
                factory_data=factory_data,
                shipping_date=shipping_date,
                extraction_status=FactoryConfirmation.ExtractionStatus.FAILED,
                extraction_error=(
                    "工厂采购文件已提取，且已保留 OCR 结果。"
                    "识别到的 bon de commande 与已选择订单不一致，"
                    "需要人工确认后才能继续生成 ShipmentBatch / Workflow。"
                ),
            )
            return factory_data

        # --------------------------------------------------------
        # 2. 检查批次顺序并创建 SerialItem
        # --------------------------------------------------------
        finalize_factory_confirmation_after_order_match(
            confirmation=confirmation,
            user=None,
        )
        confirmation.refresh_from_db()
        return get_confirmation_extracted_data(confirmation)

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
