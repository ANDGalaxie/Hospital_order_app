import json
from decimal import Decimal
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from portal.services.common import (
    address_data_from_text,
    document_url,
    file_url_safe,
    get_portal_lang,
    get_user_display_name,
    json_to_lines,
    parse_decimal_value,
    parse_int_value,
)
from portal.services.workflow_portal_service import (
    get_source_label,
    status_label,
)


def order_extraction_status(order):
    raw_status = getattr(order, "extraction_status", "") or ""
    error = getattr(order, "extraction_error", "") or ""
    extracted_at = getattr(order, "extracted_at", None)
    extracted_data = getattr(order, "extracted_order_data", None)
    confirmed_data = getattr(order, "confirmed_order_data", None)

    if error or raw_status in ["failed", "error"]:
        return "提取失败", "danger", "error"

    if extracted_at or extracted_data or confirmed_data or raw_status in ["extracted", "success", "done"]:
        return "已提取", "success", "extracted"

    return "待提取", "warning", "pending"


def order_validation_status(order):
    raw_status = getattr(order, "document_validation_status", "") or ""
    validated_at = getattr(order, "validated_at", None)
    validation_data = getattr(order, "document_validation_data", None) or {}

    errors = validation_data.get("errors") or []
    warnings = validation_data.get("warnings") or []

    if raw_status in ["failed", "error", "blocked"] or errors:
        return "有问题", "danger", "issue"

    if validated_at or raw_status in ["ready", "validated", "success", "ok"]:
        if warnings:
            return "有提醒", "warning", "warning"
        return "已验证", "success", "validated"

    return "待验证", "warning", "pending"


def get_order_item_summary(order):
    try:
        from orders.models import OrderItem
        items = list(OrderItem.objects.filter(order_id=order.id))
    except Exception:
        return "—"

    line_count = len(items)
    total_qty = 0

    for item in items:
        value = getattr(item, "requested_quantity", None)

        if isinstance(value, (int, float)):
            total_qty += value

    if line_count and total_qty:
        return f"{line_count} 行 / {int(total_qty)} 件"

    if line_count:
        return f"{line_count} 行"

    return "—"


def get_order_next_action(order, workflow_count):
    extraction_text, extraction_class, extraction_category = order_extraction_status(order)
    validation_text, validation_class, validation_category = order_validation_status(order)

    if extraction_category == "pending":
        return "提取订单", "warning"

    if extraction_category == "error":
        return "检查提取错误", "danger"

    if validation_category in ["pending", "warning"]:
        return "检查验证结果", "warning"

    if validation_category == "issue":
        return "处理异常", "danger"

    if workflow_count:
        return "查看工作流", "success"

    return "等待工厂确认", "info"


def get_order_status_label(order):
    raw_status = getattr(order, "status", "") or ""

    mapping = {
        "draft": ("草稿", "warning"),
        "pending": ("处理中", "warning"),
        "extracted": ("已提取", "info"),
        "validated": ("已验证", "success"),
        "confirmed": ("已确认", "success"),
        "completed": ("已完成", "success"),
        "error": ("有问题", "danger"),
        "failed": ("失败", "danger"),
    }

    return mapping.get(raw_status, (raw_status or "—", "info"))


def get_bon_de_commande_from_extracted_data(order):
    data = order.extracted_order_data or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    candidates = [
        data.get("summary", {}).get("bon_de_commande"),
        data.get("header", {}).get("bon_de_commande"),
        data.get("bon_de_commande"),
    ]

    for value in candidates:
        if not value:
            continue

        value_text = str(value).strip()
        digits = "".join(ch for ch in value_text if ch.isdigit())

        if 3 <= len(digits) <= 12:
            return digits

    return None


def sync_order_number_from_extracted_data(order):
    bon_de_commande = get_bon_de_commande_from_extracted_data(order)

    if not bon_de_commande:
        return False

    current_number = order.bon_de_commande or ""

    should_replace = (
        not current_number
        or current_number.startswith("UPLOAD-")
        or current_number in ["待提取", "UNKNOWN"]
    )

    if not should_replace:
        return False

    order.bon_de_commande = bon_de_commande
    order.save(update_fields=["bon_de_commande", "updated_at"])

    return True


def validate_portal_order_after_extraction(order):
    order.refresh_from_db()

    errors = []
    warnings = []

    if not order.bon_de_commande or order.bon_de_commande.startswith("UPLOAD-"):
        errors.append("没有成功提取 bon de commande。")

    if not order.hospital_name or order.hospital_name == "待提取":
        warnings.append("没有成功提取医院名称。")

    if not order.hospital_id:
        warnings.append("医院没有匹配到数据库记录。")

    if not json_to_lines(order.shipping_address_data):
        warnings.append("没有提取到收货地址。")

    if not json_to_lines(order.billing_address_data):
        warnings.append("没有提取到账单地址。")

    items = list(order.items.all())

    if not items:
        errors.append("没有提取到任何产品明细。")

    for item in items:
        if not item.product_code:
            errors.append(f"产品行 #{item.id} 没有产品号。")

        if not item.product_id:
            errors.append(f"产品 {item.product_code or item.id} 没有匹配到产品库。")

        if not item.requested_quantity or item.requested_quantity <= 0:
            warnings.append(f"产品 {item.product_code or item.id} 的订单数量异常。")

        item_label = item.product_code or item.id

        if (
            item.hospital_unit_price is None
            or item.hospital_unit_price <= 0
        ):
            warnings.append(
                f"产品 {item_label} 缺少有效医院单价。"
            )

        if (
            item.factory_unit_price is None
            or item.factory_unit_price <= 0
        ):
            warnings.append(
                f"产品 {item_label} 缺少有效工厂采购价。"
            )

        if item.product_id and not item.price_policy_id:
            warnings.append(
                f"产品 {item_label} 没有命中价格规则，"
                "当前可能仍在使用产品库默认价格。"
            )

        if (
            item.price_policy_id
            and item.expiration_discount_rate is None
        ):
            warnings.append(
                f"产品 {item_label} 缺少临期折扣率快照。"
            )

        if (
            item.price_policy_id
            and item.expiration_threshold_days is None
        ):
            warnings.append(
                f"产品 {item_label} 缺少临期门槛快照。"
            )

    validation_status = "error" if errors else "validated"

    order.document_validation_status = validation_status
    order.document_validation_data = {
        "source": "portal_order_basic_validation",
        "checked_at": timezone.now().isoformat(),
        "errors": errors,
        "warnings": warnings,
    }
    order.validated_at = timezone.now()
    order.save(
        update_fields=[
            "document_validation_status",
            "document_validation_data",
            "validated_at",
            "updated_at",
        ]
    )

    return errors, warnings


def get_latest_factory_request_document(order):
    from documents.models import GeneratedDocument

    qs = GeneratedDocument.objects.filter(document_type="factory_order_request")

    field_names = {field.name for field in GeneratedDocument._meta.fields}

    if "order" in field_names:
        qs = qs.filter(order_id=order.id)
    elif "source_order" in field_names:
        qs = qs.filter(source_order_id=order.id)
    elif "hospital_order" in field_names:
        qs = qs.filter(hospital_order_id=order.id)
    else:
        return None

    return qs.order_by("-generated_at", "-id").first()


def get_order_item_ocr_source_text(item):
    data = item.raw_data or {}

    if isinstance(data, dict):
        candidates = [
            data.get("raw_product_text"),
            data.get("raw_ref_text"),
            data.get("raw_reference_text"),
            data.get("ref_fournisseur"),
            data.get("reference"),
            data.get("product_code"),
            data.get("ocr_text"),
        ]

        for value in candidates:
            if value:
                return str(value).strip()

    return item.product_code or "—"


def build_order_list_context(request):
    from orders.models import Order
    from workflow.models import DocumentWorkflowItem

    lang = get_portal_lang(request)

    query = (request.GET.get("q") or "").strip()
    status_filter = request.GET.get("status") or "all"

    orders_qs = Order.objects.select_related(
        "hospital",
        "factory",
        "created_by",
    ).order_by("-updated_at", "-id")

    if query:
        orders_qs = orders_qs.filter(
            Q(bon_de_commande__icontains=query)
            | Q(hospital_name__icontains=query)
        )

    orders = list(orders_qs[:300])
    order_ids = [order.id for order in orders]

    workflow_counts = {}

    if order_ids:
        for item in DocumentWorkflowItem.objects.filter(order_id__in=order_ids):
            workflow_counts[item.order_id] = workflow_counts.get(item.order_id, 0) + 1

    rows = []

    for order in orders:
        extraction_text, extraction_class, extraction_category = order_extraction_status(order)
        validation_text, validation_class, validation_category = order_validation_status(order)
        order_status_text, order_status_class, order_status_category = order_combined_status(order)

        workflow_count = workflow_counts.get(order.id, 0)

        if workflow_count:
            workflow_text = f"已进入 ({workflow_count})"
            workflow_class = "success"
            workflow_category = "entered"
        else:
            workflow_text = "未进入"
            workflow_class = "warning"
            workflow_category = "not_entered"

        next_action_text, next_action_class = get_order_next_action(order, workflow_count)

        hospital_name = order.hospital_name or "—"

        if getattr(order, "hospital", None):
            hospital_name = getattr(order.hospital, "name", None) or hospital_name

        rows.append(
            {
                "id": order.id,
                "order_number": order.bon_de_commande or f"Order #{order.id}",
                "hospital_name": hospital_name,
                "item_summary": get_order_item_summary(order),
                "extraction_text": extraction_text,
                "extraction_class": extraction_class,
                "extraction_category": extraction_category,
                "validation_text": validation_text,
                "validation_class": validation_class,
                "validation_category": validation_category,
                "order_status_text": order_status_text,
                "order_status_class": order_status_class,
                "order_status_category": order_status_category,
                "workflow_text": workflow_text,
                "workflow_class": workflow_class,
                "workflow_category": workflow_category,
                "next_action_text": next_action_text,
                "next_action_class": next_action_class,
                "updated_at": order.updated_at,
                "created_at": order.created_at,
                "admin_url": f"/admin/orders/order/{order.id}/change/",
                "detail_url": reverse("portal:order_detail", args=[order.id]),
                "action_url": reverse("portal:order_action", args=[order.id]),
            }
        )

    unfiltered_rows = list(rows)

    if status_filter == "pending_extraction":
        rows = [row for row in rows if row["extraction_category"] == "pending"]
    elif status_filter == "pending_validation":
        rows = [
            row for row in rows
            if row["validation_category"] in ["pending", "warning", "issue"]
        ]
    elif status_filter == "workflow":
        rows = [row for row in rows if row["workflow_category"] == "entered"]

    return {
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
        "rows": rows,
        "query": query,
        "status_filter": status_filter,
        "stats": {
            "all": len(unfiltered_rows),
            "pending_extraction": sum(
                1 for row in unfiltered_rows
                if row["extraction_category"] == "pending"
            ),
            "pending_validation": sum(
                1 for row in unfiltered_rows
                if row["validation_category"] in ["pending", "warning", "issue"]
            ),
            "workflow": sum(
                1 for row in unfiltered_rows
                if row["workflow_category"] == "entered"
            ),
        },
    }


def build_order_detail_context(request, order_id):
    from django.shortcuts import get_object_or_404
    from orders.models import Order, OrderItem
    from workflow.models import DocumentWorkflowItem
    from portal.services.order_item_crop_service import get_order_item_row_crop_url

    lang = get_portal_lang(request)

    order = get_object_or_404(
        Order.objects.select_related(
            "hospital",
            "factory",
            "created_by",
        ),
        id=order_id,
    )

    order_items = list(
        OrderItem.objects.select_related(
            "product",
            "price_policy",
        )
        .filter(order_id=order.id)
        .order_by("id")
    )

    workflow_items = list(
        DocumentWorkflowItem.objects.select_related(
            "shipment_batch",
            "invoice_document",
            "po_document",
        )
        .filter(order_id=order.id)
        .order_by("id")
    )

    extraction_text, extraction_class, extraction_category = order_extraction_status(order)
    validation_text, validation_class, validation_category = order_validation_status(order)
    order_status_text, order_status_class = get_order_status_label(order)

    validation_data = order.document_validation_data or {}
    validation_errors = validation_data.get("errors") or []
    validation_warnings = validation_data.get("warnings") or []

    item_rows = []
    total_requested = 0

    price_policy_matched_count = 0
    estimated_hospital_total = Decimal("0.00")
    estimated_factory_total = Decimal("0.00")

    for item in order_items:
        requested_quantity = item.requested_quantity or 0
        total_requested += requested_quantity

        product_name = "—"
        matched_product_code = "—"

        if item.product:
            product_name = getattr(item.product, "description", None) or str(item.product)
            matched_product_code = getattr(item.product, "code", None) or str(item.product)

        match_status = str(item.product_match_status or "").lower()

        if match_status in ["matched", "success", "confirmed", "ok"]:
            product_match_text = "已匹配"
            product_match_class = "success"
        elif match_status in ["failed", "error", "missing"]:
            product_match_text = "未匹配"
            product_match_class = "danger"
        elif match_status in ["needs_review", "review"]:
            product_match_text = "待核对"
            product_match_class = "warning"
        elif match_status:
            product_match_text = item.product_match_status
            product_match_class = "warning"
        else:
            product_match_text = "—"
            product_match_class = "info"

        hospital_price = (
            item.hospital_unit_price
            or Decimal("0.00")
        )

        factory_price = (
            item.factory_unit_price
            or Decimal("0.00")
        )

        estimated_hospital_total += (
            hospital_price
            * requested_quantity
        )

        estimated_factory_total += (
            factory_price
            * requested_quantity
        )

        discount_percent = (
            (
                item.expiration_discount_rate
                or Decimal("0")
            )
            * Decimal("100")
        )

        if item.price_policy_id:
            price_policy_matched_count += 1
            price_source_text = (
                item.price_policy.name
                or f"PricePolicy #{item.price_policy_id}"
            )
            price_source_class = "success"

        elif (
            item.product_id
            and hospital_price > 0
        ):
            price_source_text = "产品库默认价格"
            price_source_class = "warning"

        else:
            price_source_text = "价格待处理"
            price_source_class = "danger"

        item_rows.append(
            {
                "id": item.id,
                "row_crop_url": get_order_item_row_crop_url(item),
                "ocr_source_text": get_order_item_ocr_source_text(item),
                "product_code": item.product_code or "",
                "matched_product_code": matched_product_code,
                "description": item.description or product_name or "",
                "requested_quantity": requested_quantity,
                "hospital_unit_price": item.hospital_unit_price,
                "factory_unit_price": item.factory_unit_price,
                "price_policy_id": item.price_policy_id,
                "price_policy_name": (
                    item.price_policy.name
                    if item.price_policy
                    else ""
                ),
                "price_policy_date": item.price_policy_date,
                "price_policy_message": item.price_policy_message,
                "price_source_text": price_source_text,
                "price_source_class": price_source_class,
                "discount_percent": discount_percent,
                "expiration_threshold_days": (
                    item.expiration_threshold_days
                ),
                "product_match_text": product_match_text,
                "product_match_class": product_match_class,
                "is_manually_confirmed": item.is_manually_confirmed,
                "status": item.status or "—",
            }
        )

    workflow_rows = []

    for workflow_item in workflow_items:
        workflow_text, workflow_class = status_label(workflow_item.workflow_status, "workflow")
        wf_validation_text, wf_validation_class = status_label(workflow_item.validation_status, "validation")
        invoice_text, invoice_class = status_label(workflow_item.invoice_status, "invoice")
        po_text, po_class = status_label(workflow_item.po_status, "po")

        data = workflow_item.validation_data or {}

        workflow_rows.append(
            {
                "id": workflow_item.id,
                "batch_number": data.get("batch_number"),
                "source_label": get_source_label(workflow_item),
                "workflow_text": workflow_text,
                "workflow_class": workflow_class,
                "validation_text": wf_validation_text,
                "validation_class": wf_validation_class,
                "invoice_text": invoice_text,
                "invoice_class": invoice_class,
                "po_text": po_text,
                "po_class": po_class,
                "invoice_url": document_url(workflow_item.invoice_document),
                "po_url": document_url(workflow_item.po_document),
                "detail_url": reverse("portal:workflow_detail", args=[workflow_item.id]),
            }
        )

    hospital_name = order.hospital_name or "—"

    if order.hospital:
        hospital_name = getattr(order.hospital, "name", None) or hospital_name

    factory_name = "—"

    if order.factory:
        factory_name = getattr(order.factory, "name", None) or str(order.factory)

    factory_request_document = get_latest_factory_request_document(order)

    return {
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
        "order": order,
        "order_number": order.bon_de_commande or f"Order #{order.id}",
        "hospital_name": hospital_name,
        "factory_name": factory_name,
        "order_status_text": order_status_text,
        "order_status_class": order_status_class,
        "extraction_text": extraction_text,
        "extraction_class": extraction_class,
        "validation_text": validation_text,
        "validation_class": validation_class,
        "pdf_url": file_url_safe(order.hospital_order_pdf),
        "shipping_address_lines": json_to_lines(order.shipping_address_data),
        "billing_address_lines": json_to_lines(order.billing_address_data),
        "shipping_address_text": "\n".join(json_to_lines(order.shipping_address_data)),
        "billing_address_text": "\n".join(json_to_lines(order.billing_address_data)),
        "validation_errors": validation_errors,
        "validation_warnings": validation_warnings,
        "extraction_error": order.extraction_error,
        "item_rows": item_rows,
        "item_count": len(item_rows),
        "workflow_rows": workflow_rows,
        "total_requested": total_requested,
        "price_policy_matched_count": (
            price_policy_matched_count
        ),
        "estimated_hospital_total": (
            estimated_hospital_total
        ),
        "estimated_factory_total": (
            estimated_factory_total
        ),
        "admin_url": f"/admin/orders/order/{order.id}/change/",
        "edit_url": reverse("portal:order_edit", args=[order.id]),
        "action_url": reverse("portal:order_action", args=[order.id]),
        "factory_request_url": document_url(factory_request_document),
    }


def build_order_upload_context(request):
    return {
        "lang": get_portal_lang(request),
        "user_display_name": get_user_display_name(request.user),
    }


def create_order_for_upload(uploaded_file, user):
    from orders.models import Order

    temporary_bon = f"UPLOAD-{timezone.now().strftime('%Y%m%d%H%M%S')}"

    return Order.objects.create(
        bon_de_commande=temporary_bon,
        hospital_name="待提取",
        hospital_order_pdf=uploaded_file,
        created_by=user,
    )


def run_order_extraction(order, force_ocr=False):
    from orders.services.hospital_order_extraction_service import (
        extract_hospital_order_for_order,
    )

    extract_hospital_order_for_order(
        order=order,
        force_ocr=force_ocr,
    )

    order.refresh_from_db()
    sync_order_number_from_extracted_data(order)
    order.refresh_from_db()

    extraction_text, extraction_class, extraction_category = order_extraction_status(order)

    if extraction_category in ["error", "pending"]:
        return extraction_category, [], []

    errors, warnings = validate_portal_order_after_extraction(order)
    order.refresh_from_db()

    return "ok", errors, warnings


def save_order_manual_edit(order, post_data):
    from decimal import Decimal

    from products.models import Product

    order.bon_de_commande = (
        post_data.get("bon_de_commande")
        or ""
    ).strip() or order.bon_de_commande

    order.hospital_name = (
        post_data.get("hospital_name")
        or ""
    ).strip()

    order.notes = (
        post_data.get("notes")
        or ""
    ).strip()

    order.shipping_address_data = (
        address_data_from_text(
            post_data.get(
                "shipping_address_text"
            )
        )
    )

    order.billing_address_data = (
        address_data_from_text(
            post_data.get(
                "billing_address_text"
            )
        )
    )

    order.save(
        update_fields=[
            "bon_de_commande",
            "hospital_name",
            "notes",
            "shipping_address_data",
            "billing_address_data",
            "updated_at",
        ]
    )

    for item in order.items.all():
        prefix = f"item_{item.id}_"

        old_product_id = item.product_id
        old_product_code = item.product_code or ""
        old_hospital_price = (
            item.hospital_unit_price
        )

        product_code = (
            post_data.get(
                prefix + "product_code"
            )
            or ""
        ).strip()

        description = (
            post_data.get(
                prefix + "description"
            )
            or ""
        ).strip()

        requested_quantity = parse_int_value(
            post_data.get(
                prefix + "requested_quantity"
            ),
            item.requested_quantity or 0,
        )

        posted_hospital_price = (
            parse_decimal_value(
                post_data.get(
                    prefix
                    + "hospital_unit_price"
                ),
                item.hospital_unit_price,
            )
        )

        product = None

        if product_code:
            product = Product.objects.filter(
                code=product_code,
                is_active=True,
            ).first()

        new_product_id = (
            product.id
            if product
            else None
        )

        product_changed = (
            product_code != old_product_code
            or new_product_id != old_product_id
        )

        manual_price_changed = (
            posted_hospital_price
            != old_hospital_price
        )

        item.product_code = product_code
        item.description = description
        item.requested_quantity = (
            requested_quantity
        )

        if product:
            item.product = product
            item.product_match_status = "ok"
            item.product_match_message = (
                "Matched manually from Portal."
            )
        else:
            item.product = None
            item.product_match_status = (
                "needs_review"
            )
            item.product_match_message = (
                "Product was edited manually "
                "but not found in Product database."
            )

        if product_changed:
            # 产品改变后，旧价格规则快照不能继续保留。
            if product:
                item.hospital_unit_price = (
                    product.hospital_unit_price
                    if product.hospital_unit_price
                    is not None
                    else Decimal("0.00")
                )
                item.factory_unit_price = (
                    product.factory_unit_price
                )
            else:
                item.hospital_unit_price = (
                    Decimal("0.00")
                )
                item.factory_unit_price = None

            item.expiration_discount_rate = None
            item.expiration_threshold_days = None
            item.price_policy = None
            item.price_policy_date = None
            item.price_policy_message = (
                "Product was changed manually. "
                "Please reapply PricePolicy."
            )

        else:
            item.hospital_unit_price = (
                posted_hospital_price
            )

            if manual_price_changed:
                # 医院价格已经人工覆盖，
                # 不再声称它完整来自原价格规则。
                item.price_policy = None
                item.price_policy_date = None
                item.price_policy_message = (
                    "Hospital unit price was "
                    "manually overridden in Portal. "
                    "Factory price and discount "
                    "snapshots were retained."
                )

        item.save(
            update_fields=[
                "product",
                "product_code",
                "description",
                "requested_quantity",
                "hospital_unit_price",
                "factory_unit_price",
                "expiration_discount_rate",
                "expiration_threshold_days",
                "price_policy",
                "price_policy_date",
                "price_policy_message",
                "product_match_status",
                "product_match_message",
                "updated_at",
            ]
        )

    return validate_portal_order_after_extraction(
        order
    )


def generate_factory_request_for_order(order, user):
    from documents.services.factory_order_request_service import (
        generate_factory_order_request,
    )

    errors, warnings = validate_portal_order_after_extraction(order)

    if errors:
        return None, errors, warnings

    document = generate_factory_order_request(
        order=order,
        generated_by=user,
    )

    return document, errors, warnings

def order_combined_status(order):
    """
    医院订单列表页使用的单一状态。
    合并 extraction_status + document_validation_status。
    """
    extraction_text, extraction_class, extraction_category = order_extraction_status(order)
    validation_text, validation_class, validation_category = order_validation_status(order)

    if extraction_category == "pending":
        return "待提取", "warning", "pending_extraction"

    if extraction_category == "error":
        return "提取失败", "danger", "extraction_error"

    if validation_category == "pending":
        return "待验证", "warning", "pending_validation"

    if validation_category == "warning":
        return "有提醒", "warning", "warning"

    if validation_category == "issue":
        return "有问题", "danger", "issue"

    if validation_category == "validated":
        return "已就绪", "success", "ready"

    return "待检查", "warning", "needs_review"