import json
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from hospitals.models import Hospital
from orders.models import Order, OrderItem
from products.models import Product

from legacy_services.hospital_order_extractor import (
    run_paddleocr_for_pdf,
    load_ocr_blocks,
    extract_header,
    extract_addresses,
    match_hospital,
    extract_all_items,
    validate_result,
)


def get_order_workspace(order: Order) -> Path:
    return (
        Path(settings.MEDIA_ROOT)
        / "order_workspaces"
        / f"order_{order.id}"
        / "hospital_order"
    )


def save_extracted_order_json(order: Order, data: Dict[str, Any]) -> Path:
    workspace = get_order_workspace(order)
    workspace.mkdir(parents=True, exist_ok=True)

    json_path = workspace / "extracted_order.json"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return json_path


def build_product_codes_from_db() -> List[str]:
    """
    从 Django Product 表读取合法产品编号。

    这一步替代旧版从 product_database.xlsx 读取产品编号。
    """
    return list(
        Product.objects.filter(is_active=True)
        .values_list("code", flat=True)
        .order_by("code")
    )


def build_hospitals_from_db() -> List[Dict[str, Any]]:
    """
    从 Django Hospital 表构造旧匹配函数需要的数据结构。

    这一步替代旧版从 hospital_database.xlsx 读取医院数据库。
    """
    hospitals = []

    for hospital in Hospital.objects.filter(is_active=True).order_by("name"):
        hospitals.append(
            {
                "hospital_name": hospital.name,
                "billing_address": hospital.billing_address,
                "raw_row": {
                    "id": hospital.id,
                    "name": hospital.name,
                    "billing_address": hospital.billing_address,
                    "default_shipping_address": hospital.default_shipping_address,
                    "phone": hospital.phone,
                    "fax": hospital.fax,
                    "email": hospital.email,
                },
            }
        )

    return hospitals


def extract_order_from_ocr_with_django_db(ocr_dir: Path) -> Dict[str, Any]:
    """
    使用旧 OCR 解析逻辑，但产品库和医院库来自 Django 数据库。

    不再读取：
        data/product_database.xlsx
        data/hospital_database.xlsx
    """
    product_codes = build_product_codes_from_db()
    hospitals = build_hospitals_from_db()

    blocks = load_ocr_blocks(ocr_dir)

    header = extract_header(blocks)
    addresses = extract_addresses(blocks)

    hospital_match = match_hospital(
        shipping_address=addresses["shipping_address_from_order"],
        billing_address=addresses["billing_address_from_order"],
        hospitals=hospitals,
    )

    items_result = extract_all_items(
        blocks=blocks,
        product_codes=product_codes,
    )

    result: Dict[str, Any] = {
        "header": header,
        "hospital": hospital_match,
        "addresses": {
            "shipping_address_from_order": addresses["shipping_address_from_order"],
            "billing_address_from_order": addresses["billing_address_from_order"],
            "raw_delivery_lines": addresses["raw_delivery_lines"],
            "raw_billing_lines": addresses["raw_billing_lines"],
            "billing_address_from_database": hospital_match.get(
                "billing_address_from_database"
            ),
        },
        "items": items_result["items"],
        "debug": {
            "item_extraction_pages": items_result["debug_pages"],
            "product_database_source": "django_product_table",
            "hospital_database_source": "django_hospital_table",
            "product_database_count": len(product_codes),
            "hospital_database_count": len(hospitals),
        },
    }

    result["warnings"] = validate_result(result)

    result["summary"] = {
        "bon_de_commande": header.get("bon_de_commande"),
        "order_date": header.get("order_date"),
        "hospital_match_status": hospital_match.get("match_status"),
        "hospital_match_score": hospital_match.get("match_score"),
        "matched_hospital": hospital_match.get("matched_name_in_database"),
        "shipping_status": addresses["shipping_address_from_order"].get("status"),
        "billing_status": addresses["billing_address_from_order"].get("status"),
        "item_count": len(result["items"]),
        "warning_count": len(result["warnings"]),
        "all_ok": len(result["warnings"]) == 0,
    }

    return result


def get_int_quantity(value: Any) -> int:
    if value is None:
        return 0

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def get_item_quantity(item: Dict[str, Any]) -> int:
    for key in ["final_quantity", "quantity_from_boit", "quantity_from_compte"]:
        qty = get_int_quantity(item.get(key))
        if qty > 0:
            return qty

    return 0


def build_product_match_message(item: Dict[str, Any], product_code: str) -> str:
    """
    构造产品匹配失败时给人工看的提示。
    """
    messages = []

    product_status = item.get("product_status")
    if product_status:
        messages.append(f"OCR product_status: {product_status}")

    row_warnings = item.get("row_warnings") or []
    for warning in row_warnings:
        if warning:
            messages.append(str(warning))

    suggestions = item.get("product_suggestions") or []
    if suggestions:
        suggestion_text = ", ".join(
            f"{s.get('code')} ({s.get('score')})"
            for s in suggestions
            if s.get("code")
        )
        if suggestion_text:
            messages.append(f"Suggestions: {suggestion_text}")

    if not messages:
        messages.append(
            f"Product code {product_code} was not found in Django Product database."
        )

    return "\n".join(messages)


def create_order_items_from_extracted_data(
    order: Order,
    extracted_data: Dict[str, Any],
) -> int:
    """
    根据 extracted_order_data 创建 OrderItem。

    产品匹配逻辑：
        - 如果 product_code 在 Django Product 表中存在：OK
        - 如果不存在：仍然创建 OrderItem，但标记 NEEDS_REVIEW
    """
    items = extracted_data.get("items", [])

    order.items.all().delete()

    created_count = 0

    for index, item in enumerate(items, start=1):
        product_code = (
            item.get("product_code")
            or item.get("raw_product_text")
            or f"UNKNOWN-{index}"
        )

        product_code = str(product_code).strip()

        product: Optional[Product] = Product.objects.filter(
            code=product_code,
            is_active=True,
        ).first()

        requested_quantity = get_item_quantity(item)

        if product:
            description = product.description
            hospital_unit_price = product.hospital_unit_price
            product_match_status = OrderItem.ProductMatchStatus.OK
            product_match_message = "Matched automatically with Django Product database."
        else:
            description = item.get("raw_product_text", "")
            hospital_unit_price = Decimal("270.00")
            product_match_status = OrderItem.ProductMatchStatus.NEEDS_REVIEW
            product_match_message = build_product_match_message(
                item=item,
                product_code=product_code,
            )

        OrderItem.objects.create(
            order=order,
            product=product,
            product_code=product_code,
            description=description or "",
            requested_quantity=requested_quantity,
            confirmed_quantity=0,
            backordered_quantity=requested_quantity,
            hospital_unit_price=hospital_unit_price,
            status=OrderItem.Status.REQUESTED,
            product_match_status=product_match_status,
            product_match_message=product_match_message,
            is_manually_confirmed=False,
            raw_data=item,
        )

        created_count += 1

    return created_count


def update_order_basic_fields_from_extracted_data(
    order: Order,
    extracted_data: Dict[str, Any],
) -> None:
    """
    更新 Order 基础字段和医院匹配状态。
    """
    summary = extracted_data.get("summary", {})
    header = extracted_data.get("header", {})
    hospital_match = extracted_data.get("hospital", {})
    addresses = extracted_data.get("addresses", {})

    extracted_bon = (
        summary.get("bon_de_commande")
        or header.get("bon_de_commande")
    )

    if extracted_bon and str(extracted_bon) != str(order.bon_de_commande):
        warnings = extracted_data.setdefault("warnings", [])
        warnings.append(
            f"Django Order bon_de_commande={order.bon_de_commande}, "
            f"but OCR extracted bon_de_commande={extracted_bon}. Manual review required."
        )

    matched_hospital_name = (
        summary.get("matched_hospital")
        or hospital_match.get("matched_name_in_database")
    )

    matched_raw_row = hospital_match.get("matched_raw_row") or {}
    matched_hospital_id = matched_raw_row.get("id")
    match_status = hospital_match.get("match_status")
    match_score = hospital_match.get("match_score")

    matched_hospital = None

    if matched_hospital_id:
        matched_hospital = Hospital.objects.filter(id=matched_hospital_id).first()

    if matched_hospital and match_status == "OK":
        order.hospital = matched_hospital
        order.hospital_name = matched_hospital.name
        order.hospital_match_status = Order.HospitalMatchStatus.OK
        order.hospital_match_message = (
            f"Matched automatically: {matched_hospital.name}. "
            f"Score: {match_score}"
        )

    else:
        order.hospital = None

        if matched_hospital_name and not order.hospital_name:
            order.hospital_name = matched_hospital_name

        order.hospital_match_status = Order.HospitalMatchStatus.NEEDS_REVIEW
        order.hospital_match_message = (
            "Hospital match needs manual review.\n"
            f"matched_hospital_name={matched_hospital_name}\n"
            f"match_status={match_status}\n"
            f"match_score={match_score}"
        )

    order.shipping_address_data = addresses.get("shipping_address_from_order")
    order.billing_address_data = addresses.get("billing_address_from_order")


@transaction.atomic
def extract_hospital_order_for_order(
    order: Order,
    force_ocr: bool = False,
) -> Dict[str, Any]:
    """
    为一个 Django Order 执行医院订单提取。

    现在：
        OCR 仍然复用旧逻辑；
        产品库和医院库来自 Django 数据库；
        有问题的产品行不会导致提取失败，而是标记 NEEDS_REVIEW。
    """
    if not order.hospital_order_pdf:
        raise ValueError("该订单没有上传 hospital_order_pdf。")

    pdf_path = Path(order.hospital_order_pdf.path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到医院订单 PDF：{pdf_path}")

    workspace = get_order_workspace(order)
    ocr_dir = workspace / "ocr"

    workspace.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)

    try:
        run_paddleocr_for_pdf(
            pdf_path=pdf_path,
            ocr_dir=ocr_dir,
            lang="fr",
            force_ocr=force_ocr,
        )

        extracted_data = extract_order_from_ocr_with_django_db(
            ocr_dir=ocr_dir,
        )

        update_order_basic_fields_from_extracted_data(
            order=order,
            extracted_data=extracted_data,
        )

        item_count = create_order_items_from_extracted_data(
            order=order,
            extracted_data=extracted_data,
        )

        extracted_data.setdefault("django", {})
        extracted_data["django"]["order_id"] = order.id
        extracted_data["django"]["order_item_count_created"] = item_count
        extracted_data["django"]["workspace"] = str(workspace)

        json_path = save_extracted_order_json(order, extracted_data)
        extracted_data["django"]["saved_json_path"] = str(json_path)

        order.extracted_order_data = extracted_data
        order.extraction_status = Order.ExtractionStatus.SUCCESS
        order.extraction_error = ""
        order.extracted_at = timezone.now()
        order.status = Order.Status.EXTRACTED

        order.save(
            update_fields=[
                "hospital",
                "hospital_name",
                "hospital_match_status",
                "hospital_match_message",
                "shipping_address_data",
                "billing_address_data",
                "extracted_order_data",
                "extraction_status",
                "extraction_error",
                "extracted_at",
                "status",
                "updated_at",
            ]
        )

        return extracted_data

    except Exception as exc:
        error_text = traceback.format_exc()

        order.extraction_status = Order.ExtractionStatus.FAILED
        order.extraction_error = error_text

        if hasattr(Order.Status, "EXTRACTION_FAILED"):
            order.status = Order.Status.EXTRACTION_FAILED

        order.save(
            update_fields=[
                "extraction_status",
                "extraction_error",
                "status",
                "updated_at",
            ]
        )

        raise exc