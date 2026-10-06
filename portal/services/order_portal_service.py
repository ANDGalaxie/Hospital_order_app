from portal.i18n import display_validation_message
from django.utils.translation import gettext as _
import json
from decimal import Decimal
from django.db.models import BigIntegerField, Case, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone

from portal.services.common import (
    address_data_from_text,
    document_url,
    file_url_safe,
    get_global_numeric_bon_ordinals,
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


PORTAL_ORDER_VALIDATION_SOURCE = "portal_order_basic_validation"


def get_portal_order_validation_data(order):
    """Return only Hospital Order-stage validation stored on the Order.

    Order.document_validation_data is also used by the older formal document
    generation validator. Its Factory Confirmation / Serial findings belong
    to the workflow stage and must not be presented as Hospital Order issues.
    """
    validation_data = getattr(order, "document_validation_data", None) or {}

    if validation_data.get("source") != PORTAL_ORDER_VALIDATION_SOURCE:
        return {}

    return validation_data


def get_order_product_review_state(order):
    """Describe whether extracted product rows still need human review."""
    items = list(order.items.all())

    if not items:
        return "missing"

    for item in items:
        match_status = str(item.product_match_status or "").lower()

        if (
            not item.product_code
            or not item.product_id
            or match_status in [
                "needs_review",
                "review",
                "failed",
                "error",
                "missing",
            ]
        ):
            return "needs_review"

    if all(
        item.is_manually_confirmed
        or str(item.product_match_status or "").lower() == "manually_confirmed"
        for item in items
    ):
        return "confirmed"

    return "needs_confirmation"


def has_non_product_validation_errors(order):
    validation_data = get_portal_order_validation_data(order)
    errors = validation_data.get("errors") or []
    return any(not str(error).startswith("产品") for error in errors)


def order_extraction_status(order):
    raw_status = getattr(order, "extraction_status", "") or ""
    error = getattr(order, "extraction_error", "") or ""
    extracted_at = getattr(order, "extracted_at", None)
    extracted_data = getattr(order, "extracted_order_data", None)
    confirmed_data = getattr(order, "confirmed_order_data", None)

    if error or raw_status in ["failed", "error"]:
        return _("提取失败"), "danger", "error"

    if extracted_at or extracted_data or confirmed_data or raw_status in ["extracted", "success", "done"]:
        return _("已提取"), "success", "extracted"

    return _("待提取"), "warning", "pending"


def order_validation_status(order):
    raw_status = getattr(order, "document_validation_status", "") or ""
    validation_data = get_portal_order_validation_data(order)

    # A formal document/workflow validation may have written Serial findings
    # to the shared Order fields. They are deliberately ignored by this
    # Hospital Order-stage presentation.
    if not validation_data:
        return _("待验证"), "warning", "pending"

    validated_at = getattr(order, "validated_at", None)

    errors = validation_data.get("errors") or []
    warnings = validation_data.get("warnings") or []

    if raw_status in ["failed", "error", "blocked"] or errors:
        return _("有问题"), "danger", "issue"

    if validated_at or raw_status in ["ready", "validated", "success", "ok"]:
        if warnings:
            return _("有提醒"), "warning", "warning"
        return _("已验证"), "success", "validated"

    return _("待验证"), "warning", "pending"


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
        return _('%(value1)s 行 / %(value2)s 件') % {'value1': line_count, 'value2': int(total_qty)}

    if line_count:
        return _('%(value1)s 行') % {'value1': line_count}

    return "—"


def get_order_next_action(order, workflow_count):
    extraction_text, extraction_class, extraction_category = order_extraction_status(order)
    validation_text, validation_class, validation_category = order_validation_status(order)

    if extraction_category == "pending":
        return _("提取订单"), "warning"

    if extraction_category == "error":
        return _("检查提取错误"), "danger"

    if validation_category == "issue" and has_non_product_validation_errors(order):
        return _("处理异常"), "danger"

    product_review_state = get_order_product_review_state(order)

    if product_review_state in ["missing", "needs_review", "needs_confirmation"]:
        return _("请核对产品编码"), "warning"

    if workflow_count:
        return _("查看工作流"), "success"

    return _("等待工厂确认"), "info"


def get_order_status_label(order):
    raw_status = getattr(order, "status", "") or ""

    mapping = {
        "draft": (_("草稿"), "warning"),
        "pending": (_("处理中"), "warning"),
        "extracted": (_("已提取"), "info"),
        "validated": (_("已验证"), "success"),
        "confirmed": (_("已确认"), "success"),
        "completed": (_("已完成"), "success"),
        "error": (_("有问题"), "danger"),
        "failed": (_("失败"), "danger"),
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

    if not order.order_date:
        warnings.append("没有成功提取医院订单日期。")

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
            errors.append(
                f"产品 {item.product_code or item.id} 没有匹配到产品库，"
                "请核对产品编码。"
            )
        elif str(item.product_match_status or "").lower() in [
            "needs_review",
            "review",
        ]:
            warnings.append(
                f"产品 {item.product_code or item.id} 的产品编码待核对。"
            )

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

        if item.product_id and not item.price_policy_id:
            warnings.append(
                f"产品 {item_label} 尚未应用医院价格规则，"
                "请重新应用 PricePolicy。"
            )


    validation_status = "error" if errors else "validated"

    order.document_validation_status = validation_status
    order.document_validation_data = {
        "source": PORTAL_ORDER_VALIDATION_SOURCE,
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

    numeric_bon_condition = Q(bon_de_commande__regex=r"^[0-9]+$")

    orders_qs = (
        Order.objects.select_related(
            "hospital",
            "factory",
            "created_by",
        )
        .prefetch_related("items")
        .annotate(
            bon_is_numeric=Case(
                When(numeric_bon_condition, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            bon_numeric=Case(
                When(
                    numeric_bon_condition,
                    then=Cast("bon_de_commande", BigIntegerField()),
                ),
                default=Value(None),
                output_field=BigIntegerField(),
            ),
        )
        .order_by("-bon_is_numeric", "-bon_numeric", "-id")
    )

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

    bon_ordinals = get_global_numeric_bon_ordinals()
    rows = []

    for order in orders:
        extraction_text, extraction_class, extraction_category = order_extraction_status(order)
        validation_text, validation_class, validation_category = order_validation_status(order)
        order_status_text, order_status_class, order_status_category = order_combined_status(order)

        workflow_count = workflow_counts.get(order.id, 0)

        if workflow_count:
            workflow_text = _('已进入 (%(value1)s)') % {'value1': workflow_count}
            workflow_class = "success"
            workflow_category = "entered"
        else:
            workflow_text = _("未进入")
            workflow_class = "warning"
            workflow_category = "not_entered"

        next_action_text, next_action_class = get_order_next_action(order, workflow_count)

        hospital_name = order.hospital_name or "—"

        if getattr(order, "hospital", None):
            hospital_name = getattr(order.hospital, "name", None) or hospital_name

        rows.append(
            {
                "id": order.id,
                "bon_ordinal": bon_ordinals.get(order.id),
                "order_date": order.order_date,
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
    from shipments.models import ShipmentBatch
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

    validation_data = get_portal_order_validation_data(order)
    validation_errors = [display_validation_message(value) for value in (validation_data.get("errors") or [])]
    validation_warnings = [display_validation_message(value) for value in (validation_data.get("warnings") or [])]

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
            product_match_text = _("已匹配")
            product_match_class = "success"
        elif match_status in ["failed", "error", "missing"]:
            product_match_text = _("未匹配")
            product_match_class = "danger"
        elif match_status in ["needs_review", "review"]:
            product_match_text = _("待核对")
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
            price_source_text = _("产品库默认价格")
            price_source_class = "warning"

        else:
            price_source_text = _("价格待处理")
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

    shipment_batches = list(
        ShipmentBatch.objects.filter(order=order).order_by("-batch_number", "-id")
    )
    latest_batch = shipment_batches[0] if shipment_batches else None
    total_shipped = sum(
        int(item.confirmed_quantity or 0)
        for item in order_items
    )
    total_remaining = sum(
        int(item.backordered_quantity or 0)
        for item in order_items
    )

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
        "total_shipped": total_shipped,
        "total_remaining": total_remaining,
        "latest_batch": latest_batch,
        "backorder_total": total_remaining,
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

    from orders.services.order_price_policy_service import (
        reapply_order_price_policy,
    )
    from products.models import Product

    product_changed_any = False

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
            item.product_match_status = "manually_confirmed"
            item.product_match_message = (
                "Matched manually from Portal."
            )
            item.is_manually_confirmed = True
        else:
            item.product = None
            item.product_match_status = (
                "needs_review"
            )
            item.product_match_message = (
                "Product was edited manually "
                "but not found in Product database."
            )
            item.is_manually_confirmed = False

        if product_changed:
            product_changed_any = True

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
                "is_manually_confirmed",
                "updated_at",
            ]
        )

    reapply_warning = ""

    if product_changed_any:
        try:
            reapply_order_price_policy(order)
        except ValueError as exc:
            # 正式 Invoice / PO 等保护条件不应阻断
            # 用户已完成的产品信息修正。
            reapply_warning = (
                _('自动重新应用医院 PricePolicy 失败：%(value1)s') % {'value1': exc}
            )

    order.refresh_from_db()
    errors, warnings = (
        validate_portal_order_after_extraction(order)
    )

    if reapply_warning:
        warnings.append(reapply_warning)
        validation_data = dict(
            order.document_validation_data or {}
        )
        validation_data["warnings"] = warnings
        order.document_validation_data = validation_data
        order.save(
            update_fields=[
                "document_validation_data",
                "updated_at",
            ]
        )

    return errors, warnings


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
        return _("待提取"), "warning", "pending_extraction"

    if extraction_category == "error":
        return _("提取失败"), "danger", "extraction_error"

    if validation_category == "issue" and has_non_product_validation_errors(order):
        return _("有问题"), "danger", "issue"

    if get_order_product_review_state(order) in [
        "missing",
        "needs_review",
        "needs_confirmation",
    ]:
        return _("产品编码待核对"), "warning", "product_review"

    if validation_category == "pending":
        return _("待验证"), "warning", "pending_validation"

    if validation_category == "warning":
        return _("有提醒"), "warning", "warning"

    if validation_category == "validated":
        return _("已就绪"), "success", "ready"

    return _("待检查"), "warning", "needs_review"
