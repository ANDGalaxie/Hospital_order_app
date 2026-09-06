"""One-off, auditable repair support for hospital order 150222 only."""

import copy
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.models import DocumentSequence, GeneratedDocument
from orders.models import Order
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_document_generation_service import (
    generate_hospital_invoice_for_workflow_item,
)


TARGET_BON = "150222"
OLD_BASE_INVOICE_NUMBER = "Invoice 20260105"
NEW_BASE_INVOICE_NUMBER = "Invoice 20260505"
PRODUCT_CODE_RE = re.compile(r"^BMA-\d\.\d{4}$")
UNIT_PRICE_RE = re.compile(r"^(270|300)\.0+$")
LINE_TOTAL_RE = re.compile(r"^\d+\.\d{2}$")


def _to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ocr_workspace(order):
    data = order.extracted_order_data or {}
    django_data = data.get("django") if isinstance(data, dict) else {}
    workspace = django_data.get("workspace") if isinstance(django_data, dict) else ""
    if workspace:
        return Path(workspace)
    return Path(settings.MEDIA_ROOT) / "order_workspaces" / f"order_{order.id}" / "hospital_order"


def _row_center(box):
    return (float(box[1]) + float(box[3])) / 2


def _read_original_order_prices(order):
    """Read prices from saved OCR coordinates, not current OrderItem prices."""
    rows = {}
    errors = []
    ocr_dir = _ocr_workspace(order) / "ocr"
    page_files = sorted(ocr_dir.glob("page_*_res.json"))
    if not page_files:
        return rows, ["找不到原始医院订单 OCR 页面数据。"]

    for page_file in page_files:
        try:
            payload = json.loads(page_file.read_text(encoding="utf-8"))
            tokens = payload["rec_texts"]
            boxes = payload["rec_boxes"]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"无法读取 OCR 文件 {page_file.name}: {exc}")
            continue

        products = [
            (text, _row_center(box))
            for text, box in zip(tokens, boxes)
            if PRODUCT_CODE_RE.fullmatch(str(text).strip())
        ]
        prices = [
            (_to_decimal(text), _row_center(box))
            for text, box in zip(tokens, boxes)
            if (UNIT_PRICE_RE.fullmatch(str(text).strip()) and float(box[0]) < 1600)
        ]
        totals = [
            (_to_decimal(text), _row_center(box))
            for text, box in zip(tokens, boxes)
            if LINE_TOTAL_RE.fullmatch(str(text).strip())
        ]

        for product_code, product_y in products:
            nearby_prices = [
                (value, abs(y - product_y))
                for value, y in prices
                if abs(y - product_y) <= 45
            ]
            if len(nearby_prices) != 1:
                errors.append(
                    f"{product_code}: 原始 OCR 价格不能唯一匹配。"
                )
                continue
            unit_price, _ = nearby_prices[0]
            if unit_price not in {Decimal("270"), Decimal("300")}:
                errors.append(f"{product_code}: OCR 单价 {unit_price} 不受支持。")
                continue
            rows[product_code] = {
                "product_code": product_code,
                "unit_price": unit_price,
                "page": page_file.name,
                "y": product_y,
                "nearby_totals": [
                    value
                    for value, y in totals
                    if abs(y - product_y) <= 45
                ],
            }

    return rows, errors


def _target_invoice_number(batch):
    suffix = f"-B{batch.batch_number}" if int(batch.batch_number or 1) > 1 else ""
    return f"{NEW_BASE_INVOICE_NUMBER}{suffix}"


def _file_fingerprint(document):
    if not document or not document.pdf_file:
        return {"path": "", "sha256": ""}
    path = Path(settings.MEDIA_ROOT) / document.pdf_file.name
    if not path.exists():
        return {"path": str(path), "sha256": ""}
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def build_order_150222_repair_plan():
    orders = list(Order.objects.filter(bon_de_commande=TARGET_BON).order_by("id"))
    if len(orders) != 1:
        return {
            "order": None,
            "errors": [f"bon_de_commande={TARGET_BON} 找到 {len(orders)} 个 Order，必须恰好一个。"],
        }
    order = orders[0]
    price_rows, errors = _read_original_order_prices(order)
    order_items = list(order.items.order_by("product_code", "id"))
    item_by_code = {item.product_code: item for item in order_items}

    if len(item_by_code) != len(order_items):
        errors.append("OrderItem product_code 不唯一，拒绝自动修复。")
    if set(item_by_code) != set(price_rows):
        missing_prices = sorted(set(item_by_code) - set(price_rows))
        extra_prices = sorted(set(price_rows) - set(item_by_code))
        if missing_prices:
            errors.append("原始 OCR 缺少产品价格：" + ", ".join(missing_prices))
        if extra_prices:
            errors.append("OCR 含非 OrderItem 产品：" + ", ".join(extra_prices))

    changes = []
    for product_code, item in item_by_code.items():
        source = price_rows.get(product_code)
        if not source:
            continue
        quantity = _to_decimal(item.requested_quantity)
        if quantity is None or quantity <= 0:
            errors.append(f"{product_code}: requested_quantity 无效。")
            continue
        expected_total = quantity * source["unit_price"]
        if expected_total not in source["nearby_totals"]:
            errors.append(
                f"{product_code}: 原始 OCR 中 quantity × unit_price 无法与 line_total 校验。"
            )
            continue
        changes.append({
            "order_item_id": item.id,
            "product_code": product_code,
            "quantity": str(quantity),
            "old_unit_price": str(item.hospital_unit_price),
            "new_unit_price": str(source["unit_price"]),
            "line_total": str(expected_total),
            "source_page": source["page"],
        })

    batches = list(
        ShipmentBatch.objects.filter(order=order)
        .prefetch_related("shipped_items")
        .order_by("batch_number", "id")
    )
    batches = [batch for batch in batches if batch.shipped_items.exists()]
    invoices = list(
        GeneratedDocument.objects.filter(
            order=order,
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
        ).order_by("id")
    )
    sequence = DocumentSequence.objects.filter(
        month_key="2026-05", bon_de_commande=TARGET_BON
    ).first()
    if not sequence:
        errors.append("找不到 150222 的 2026-05 DocumentSequence。")
    elif sequence.invoice_number != OLD_BASE_INVOICE_NUMBER:
        errors.append(
            f"DocumentSequence.invoice_number={sequence.invoice_number}，预期 {OLD_BASE_INVOICE_NUMBER}。"
        )

    current_by_batch = {doc.shipment_batch_id: doc for doc in invoices}
    target_numbers = {
        _target_invoice_number(batch)
        for batch in batches
    }
    conflicts = list(
        GeneratedDocument.objects.filter(
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
            document_number__in=target_numbers,
        )
        .exclude(order=order)
        .values("id", "order_id", "shipment_batch_id", "document_number")
    )
    if conflicts:
        errors.append("目标 Invoice 编号已被其他订单占用。")

    return {
        "order": order,
        "order_id": order.id,
        "order_date": order.order_date,
        "hospital_order_pdf": order.hospital_order_pdf.name,
        "price_rows": changes,
        "batches": [
            {
                "id": batch.id,
                "batch_number": batch.batch_number,
                "source_type": batch.source_type,
                "invoice_id": getattr(current_by_batch.get(batch.id), "id", None),
                "current_invoice_number": getattr(current_by_batch.get(batch.id), "document_number", ""),
                "target_invoice_number": _target_invoice_number(batch),
                "file": _file_fingerprint(current_by_batch.get(batch.id)),
            }
            for batch in batches
        ],
        "sequence_id": getattr(sequence, "id", None),
        "current_base_invoice_number": getattr(sequence, "invoice_number", ""),
        "target_base_invoice_number": NEW_BASE_INVOICE_NUMBER,
        "conflicts": conflicts,
        "errors": errors,
        "can_apply": not errors,
    }


@transaction.atomic
def apply_order_150222_repair(plan):
    raise ValueError("Order 150222 price repair is paused by current business rule.")
    if not plan["can_apply"]:
        raise ValueError("dry-run 存在 blocker，拒绝 apply。")
    order = Order.objects.select_for_update().get(id=plan["order_id"])
    if order.bon_de_commande != TARGET_BON:
        raise ValueError("目标订单 bon 不匹配。")

    sequence = DocumentSequence.objects.select_for_update().get(id=plan["sequence_id"])
    sequence.invoice_number = NEW_BASE_INVOICE_NUMBER
    sequence.save(update_fields=["invoice_number", "updated_at"])

    now = timezone.now().isoformat()
    changes = {row["order_item_id"]: row for row in plan["price_rows"]}
    for item in order.items.select_for_update().filter(id__in=changes):
        change = changes[item.id]
        raw_data = copy.deepcopy(item.raw_data or {})
        raw_data["manual_hospital_price_override"] = {
            "bon_de_commande": TARGET_BON,
            "source": "original_hospital_order",
            "reason": "Use product-level price printed on hospital order",
            "old_unit_price": change["old_unit_price"],
            "new_unit_price": change["new_unit_price"],
            "applied_at": now,
            "applied_by": "repair_order_150222_invoice_data",
        }
        item.hospital_unit_price = Decimal(change["new_unit_price"])
        item.raw_data = raw_data
        item.save(update_fields=["hospital_unit_price", "raw_data"])

    extracted = copy.deepcopy(order.extracted_order_data or {})
    django_data = extracted.setdefault("django", {})
    django_data["manual_hospital_price_override"] = {
        "bon_de_commande": TARGET_BON,
        "source_pdf": order.hospital_order_pdf.name,
        "prices": {row["product_code"]: row["new_unit_price"] for row in plan["price_rows"]},
        "invoice_number_change": {
            "old": OLD_BASE_INVOICE_NUMBER,
            "new": NEW_BASE_INVOICE_NUMBER,
        },
        "applied_at": now,
        "applied_by": "repair_order_150222_invoice_data",
    }
    order.extracted_order_data = extracted
    order.save(update_fields=["extracted_order_data", "updated_at"])

    generated = []
    for batch_info in plan["batches"]:
        batch = ShipmentBatch.objects.select_for_update().get(id=batch_info["id"])
        workflow_item = DocumentWorkflowItem.objects.select_for_update().get(shipment_batch=batch)
        document = GeneratedDocument.objects.select_for_update().filter(
            shipment_batch=batch,
            document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
        ).first()
        if document:
            document.document_number = batch_info["target_invoice_number"]
            document.save(update_fields=["document_number"])
        generated_by = document.generated_by if document else order.created_by
        result = generate_hospital_invoice_for_workflow_item(
            item=workflow_item,
            generated_by=generated_by,
            force_regenerate=True,
            existing_document_override=document,
        )
        workflow_item.invoice_document = result["generated_document"]
        workflow_item.invoice_status = DocumentWorkflowItem.DocumentStatus.GENERATED
        workflow_item.save(update_fields=["invoice_document", "invoice_status", "updated_at"])
        generated.append(result)
    return generated
