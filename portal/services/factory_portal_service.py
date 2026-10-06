from portal.i18n import display_choice, display_choices
from django.utils.translation import gettext as _
from collections import Counter
from urllib.parse import urlencode

from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from factory_confirmations.models import FactoryConfirmation
from orders.models import Order
from shipments.models import ShipmentBatch, ShipmentBatchItem
from workflow.models import DocumentWorkflowItem
from documents.models import GeneratedDocument

from factories.models import Factory
from factory_confirmations.services.factory_confirmation_extraction_service import (
    extract_factory_confirmation_for_confirmation,
    finalize_factory_confirmation_after_order_match,
    recalculate_order_items_from_shipment_batches,
)
from datetime import datetime

from products.models import Product
from portal.services.common import get_global_numeric_bon_ordinals
from portal.services.factory_serial_crop_service import (
    get_serial_row_crop_url,
    get_serial_source_text,
)
try:
    from backorders.services.backorder_sync_service import sync_backorders_for_order
except Exception:
    sync_backorders_for_order = None

from django.db import transaction
from django.db.models import BooleanField, Case, Count, Q, Value, When


MISSING_BON_WARNING_MARKERS = (
    "没有提取到 BON DE COMMANDE 编号",
    "未能从工厂采购文件中识别 bon de commande",
)

SUCCESSFUL_BON_MATCH_STATUSES = {
    "matched_by_detected_bon_de_commande",
    "selected_order_confirmed_by_detected_bon",
}

# Optional explicit workflow lets the list use strict eager-loaded relations;
# legacy detail callers retain their existing fallback.
_WORKFLOW_LOOKUP = object()
PENDING_EXTRACTION_STATES = (
    FactoryConfirmation.ExtractionStatus.NOT_STARTED, "pending", "processing",
)


def factory_list_status_filters():
    """List-only facts. Pending extraction and unmatched orders can overlap."""
    return {
        "pending": Q(extraction_status__in=PENDING_EXTRACTION_STATES),
        "needs_action": (
            Q(extraction_status=FactoryConfirmation.ExtractionStatus.FAILED)
            | Q(order__isnull=True)
            | Q(
                extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
                shipment_batch__document_workflow_item__isnull=True,
            )
        ),
        "workflow": Q(shipment_batch__document_workflow_item__isnull=False),
    }


def filter_resolved_bon_warnings(confirmation, django_data, warnings):
    """Hide stale extraction warnings without changing the raw OCR payload."""
    canonical_bon_is_known = bool(
        django_data.get("detected_bon_de_commande")
        or confirmation.bon_de_commande_manual_confirmed
        or django_data.get("order_match_status")
        in SUCCESSFUL_BON_MATCH_STATUSES
    )

    if not canonical_bon_is_known:
        return list(warnings or [])

    return [
        warning
        for warning in (warnings or [])
        if not any(
            marker.casefold() in str(warning).casefold()
            for marker in MISSING_BON_WARNING_MARKERS
        )
    ]


def factory_status_label(value):
    value = str(value or "").lower()

    mapping = {
        "success": (_("已提取"), "success"),
        "failed": (_("提取失败"), "danger"),
        "not_started": (_("待提取"), "warning"),
        "pending": (_("待提取"), "warning"),
        "processing": (_("处理中"), "info"),
    }

    return mapping.get(value, (value or _("待提取"), "warning"))


def order_match_label(confirmation):
    if confirmation.order_id:
        return _("已匹配"), "success"

    data = confirmation.extracted_confirmation_data or {}
    django_data = data.get("django") or {}
    detected_bon = django_data.get("detected_bon_de_commande")

    if detected_bon:
        return _('识别到 %(value1)s，未匹配') % {'value1': detected_bon}, "warning"

    return _("未匹配"), "danger"


def get_confirmation_order_number(confirmation):
    if confirmation.order_id and confirmation.order:
        return confirmation.order.bon_de_commande or f"Order #{confirmation.order_id}"

    data = confirmation.extracted_confirmation_data or {}
    django_data = data.get("django") or {}
    detected_bon = django_data.get("detected_bon_de_commande")

    return detected_bon or "—"


def get_factory_name(confirmation):
    if confirmation.factory_id and confirmation.factory:
        return getattr(confirmation.factory, "name", None) or str(confirmation.factory)

    return "—"


def get_serial_summary(confirmation):
    serial_count = confirmation.serial_items.count()

    product_count = (
        confirmation.serial_items
        .exclude(product_code="")
        .values("product_code")
        .distinct()
        .count()
    )

    return product_count, serial_count


def get_shipment_batch(confirmation):
    if "shipment_batch" in confirmation._state.fields_cache:
        return confirmation._state.fields_cache["shipment_batch"]
    return (
        ShipmentBatch.objects
        .filter(factory_confirmation=confirmation)
        .order_by("-id")
        .first()
    )


def get_batch_workflow_item(confirmation):
    """Strict batch relation only; never borrow another batch's workflow."""
    batch = get_shipment_batch(confirmation)
    if batch is None:
        return None
    try:
        return batch.document_workflow_item
    except DocumentWorkflowItem.DoesNotExist:
        return None


def get_workflow_item(confirmation):
    """Legacy status/detail lookup retains the order-level fallback."""
    item = get_batch_workflow_item(confirmation)
    if item:
        return item

    if confirmation.order_id:
        return (
            DocumentWorkflowItem.objects
            .filter(order_id=confirmation.order_id)
            .order_by("-id")
            .first()
        )

    return None


def workflow_label(confirmation, *, item=_WORKFLOW_LOOKUP):
    if item is _WORKFLOW_LOOKUP:
        item = get_workflow_item(confirmation)

    if item:
        return _("已进入"), "success"

    if confirmation.order_id and confirmation.extraction_status == FactoryConfirmation.ExtractionStatus.SUCCESS:
        return _("未进入"), "warning"

    return _("未进入"), "muted"


def next_action_label(confirmation, *, item=_WORKFLOW_LOOKUP):
    if not confirmation.order_id:
        return _("人工确认订单"), "warning"

    if confirmation.extraction_status == FactoryConfirmation.ExtractionStatus.FAILED:
        return _("检查错误"), "danger"

    if confirmation.extraction_status != FactoryConfirmation.ExtractionStatus.SUCCESS:
        return _("执行提取"), "info"

    if item is _WORKFLOW_LOOKUP:
        item = get_workflow_item(confirmation)

    if item:
        return _("查看工作流"), "success"

    return _("同步后续流程"), "warning"


def build_factory_list_context(request):
    query = (request.GET.get("q") or "").strip()
    filters = factory_list_status_filters()
    status_filter = request.GET.get("status") or "all"
    if status_filter not in {"all", *filters}:
        status_filter = "all"

    dataset = FactoryConfirmation.objects.all()
    # Database counts for the whole dataset, independent of search like Workflow.
    stats = dataset.aggregate(
        all=Count("pk"),
        **{name: Count("pk", filter=condition) for name, condition in filters.items()},
    )
    confirmations = dataset.select_related(
        "order", "factory", "created_by", "shipment_batch__document_workflow_item",
    ).annotate(
        **{
            "portal_" + name: Case(
                When(condition, then=Value(True)), default=Value(False),
                output_field=BooleanField(),
            )
            for name, condition in filters.items()
        },
        portal_serial_count=Count("serial_items", distinct=True),
        portal_product_count=Count(
            "serial_items__product_code", distinct=True,
            filter=~Q(serial_items__product_code=""),
        ),
    ).order_by("-created_at", "-id")

    bon_ordinals = get_global_numeric_bon_ordinals()
    rows = []

    for confirmation in confirmations:
        extraction_text, extraction_class = factory_status_label(
            confirmation.extraction_status
        )
        match_text, match_class = order_match_label(confirmation)
        batch = get_shipment_batch(confirmation)
        batch_workflow = get_batch_workflow_item(confirmation)
        workflow_text, workflow_class = workflow_label(confirmation, item=batch_workflow)
        next_text, next_class = next_action_label(confirmation, item=batch_workflow)
        status_text, status_class = factory_combined_status(confirmation)
        product_count, serial_count = confirmation.portal_product_count, confirmation.portal_serial_count

        tracking_url = None
        workflow_detail_url = None
        show_next_hint = True
        if confirmation.order_id:
            if batch:
                tracking_url = reverse("portal:shipment_tracking_edit", args=[batch.pk])
            if batch_workflow:
                workflow_detail_url = reverse("portal:workflow_detail", args=[batch_workflow.pk])
            if confirmation.extraction_status == FactoryConfirmation.ExtractionStatus.SUCCESS:
                # The next-action link must describe this batch, not legacy fallback.
                if batch_workflow:
                    show_next_hint = False
                else:
                    next_text, next_class = _("同步后续流程"), "warning"

        rows.append({
            "id": confirmation.id,
            "row_status": frozenset(
                name for name in filters if getattr(confirmation, "portal_" + name)
            ),
            "bon_ordinal": bon_ordinals.get(confirmation.order_id),
            "tracking_url": tracking_url,
            "tracking_number": batch.tracking_number if batch else "",
            "tracking_label": _("修改快递单号") if batch and batch.tracking_number else _("填写快递单号"),
            "workflow_detail_url": workflow_detail_url,
            "show_next_hint": show_next_hint,
            "detail_url": reverse("portal:factory_detail", args=[confirmation.id]),
            "order_number": get_confirmation_order_number(confirmation),
            "factory_name": get_factory_name(confirmation),
            "shipping_date": confirmation.shipping_date,
            "created_at": confirmation.created_at,
            "updated_at": confirmation.updated_at,
            "extraction_text": extraction_text,
            "extraction_class": extraction_class,
            "match_text": match_text,
            "match_class": match_class,
            "workflow_text": workflow_text,
            "workflow_class": workflow_class,
            "next_text": next_text,
            "next_class": next_class,
            "status_text": status_text,
            "status_class": status_class,
            "product_count": product_count,
            "serial_count": serial_count,
        })

    if query:
        term = query.casefold()
        rows = [
            row for row in rows
            if term in str(row["order_number"]).casefold()
            or term in row["factory_name"].casefold()
        ]
    if status_filter != "all":
        rows = [row for row in rows if status_filter in row["row_status"]]

    labels = {
        "all": _("全部采购文件"), "pending": _("待提取"),
        "needs_action": _("待处理"), "workflow": _("已进入工作流"),
    }
    cards = []
    for value, label in labels.items():
        params = {"status": value}
        if query:
            params["q"] = query
        cards.append({
            "label": label, "count": stats[value], "active": status_filter == value,
            "url": "?" + urlencode(params),
        })
    return {
        "rows": rows, "stats": stats, "cards": cards,
        "query": query, "status_filter": status_filter,
        "total_count": stats["all"], "upload_url": reverse("portal:factory_upload"),
    }


def build_factory_detail_context(request, confirmation_id):
    confirmation = get_object_or_404(
        FactoryConfirmation.objects.select_related(
            "order",
            "factory",
            "created_by",
            "bon_de_commande_manual_confirmed_by",
        ),
        id=confirmation_id,
    )

    extraction_text, extraction_class = factory_status_label(
        confirmation.extraction_status
    )
    match_text, match_class = order_match_label(confirmation)
    workflow_text, workflow_class = workflow_label(confirmation)

    serial_items = (
        confirmation.serial_items
        .select_related("product", "order")
        .order_by("product_code", "serial_number")
    )

    serial_rows = []
    product_counter = Counter()

    for serial in serial_items:
        product_counter[serial.product_code] += 1

        serial_rows.append({
            "id": serial.id,
            "row_crop_url": get_serial_row_crop_url(serial),
            "source_text": get_serial_source_text(serial),
            "product_code": serial.product_code or "",
            "serial_number": serial.serial_number or "",
            "expiration_date": serial.expiration_date,
            "discount_rate": serial.discount_rate,
            "product": serial.product,
        })

    order_items_by_code = {}

    if confirmation.order_id:
        for item in confirmation.order.items.all():
            order_items_by_code[item.product_code] = item

    summary_rows = []

    for product_code, confirmed_qty in sorted(product_counter.items()):
        order_item = order_items_by_code.get(product_code)
        requested_qty = int(getattr(order_item, "requested_quantity", 0) or 0)
        diff = confirmed_qty - requested_qty

        if not order_item:
            status_text = _("订单中没有该产品")
            status_class = "danger"
        elif diff == 0:
            status_text = _("数量一致")
            status_class = "success"
        elif diff < 0:
            status_text = _("部分发货")
            status_class = "warning"
        else:
            status_text = _("超发")
            status_class = "danger"

        summary_rows.append({
            "product_code": product_code,
            "requested_qty": requested_qty,
            "confirmed_qty": confirmed_qty,
            "diff": diff,
            "status_text": status_text,
            "status_class": status_class,
        })

    shipment_batch = get_shipment_batch(confirmation)
    workflow_item = get_workflow_item(confirmation)
    generated_documents = []

    if shipment_batch:
        generated_documents = list(
            GeneratedDocument.objects.filter(
                shipment_batch=shipment_batch,
            ).order_by("-generated_at", "-id")
        )

    data = confirmation.extracted_confirmation_data or {}
    django_data = data.get("django") or {}
    warnings = filter_resolved_bon_warnings(
        confirmation,
        django_data,
        data.get("warnings") or [],
    )
    detected_bon = django_data.get("detected_bon_de_commande")
    audit_messages = []

    if confirmation.bon_de_commande_manual_confirmed and confirmation.order_id:
        if not detected_bon:
            audit_messages.append(
                _("PDF 中未识别到 bon de commande，系统按所选医院订单继续处理。")
            )
        elif detected_bon != confirmation.order.bon_de_commande:
            audit_messages.append(
                _('PDF 识别编号为 %(value1)s，系统最终按医院订单 %(value2)s 继续处理。') % {'value1': detected_bon, 'value2': confirmation.order.bon_de_commande}
            )
    requires_manual_confirmation = bool(
        django_data.get("requires_manual_confirmation")
    )
    can_finalize = bool(
        confirmation.order_id
        and (
            confirmation.extraction_status
            != FactoryConfirmation.ExtractionStatus.SUCCESS
            or requires_manual_confirmation
            or not shipment_batch
        )
    )

    pdf_url = None
    if confirmation.confirmation_pdf:
        try:
            pdf_url = confirmation.confirmation_pdf.url
        except Exception:
            pdf_url = None

    return {
        "confirmation": confirmation,
        "order_number": get_confirmation_order_number(confirmation),
        "factory_name": get_factory_name(confirmation),
        "pdf_url": pdf_url,
        "extraction_text": extraction_text,
        "extraction_class": extraction_class,
        "match_text": match_text,
        "match_class": match_class,
        "workflow_text": workflow_text,
        "workflow_class": workflow_class,
        "serial_rows": serial_rows,
        "summary_rows": summary_rows,
        "serial_count": serial_items.count(),
        "product_count": len(product_counter),
        "shipment_batch": shipment_batch,
        "workflow_item": workflow_item,
        "generated_documents": generated_documents,
        "django_data": django_data,
        "warnings": warnings,
        "audit_messages": audit_messages,
        "order_choices": Order.objects.order_by("bon_de_commande"),
        "requires_manual_confirmation": requires_manual_confirmation,
        "can_finalize": can_finalize,
    }

def build_factory_upload_context(
    request,
    *,
    selected_order_id=None,
    default_confirmation_type=None,
    order_locked=False,
):
    selected_order = None

    if selected_order_id:
        selected_order = (
            Order.objects.select_related("factory")
            .filter(id=selected_order_id)
            .first()
        )

    return {
        "confirmation_type_choices": [("auto", _("自动识别"))] + list(display_choices(FactoryConfirmation.ConfirmationType.choices)),
        "order_choices": Order.objects.order_by("bon_de_commande"),
        "selected_order": selected_order,
        "selected_order_id": selected_order.id if selected_order else "",
        "order_locked": order_locked,
        "default_confirmation_type": (
            default_confirmation_type
            or "auto"
        ),
    }


def create_and_extract_factory_confirmation(
    *,
    uploaded_file,
    confirmation_type,
    order_id,
    user,
):
    """
    Portal 上传工厂确认 PDF 的正式入口。

    流程：
        1. 创建 FactoryConfirmation，order 先为空
        2. 调用底层提取 service
        3. 底层 service 自动识别 bon de commande 并匹配 Order
        4. 成功后创建 SerialItem / ShipmentBatch / Workflow
    """
    order = None
    if order_id:
        order = (
            Order.objects
            .select_related("factory")
            .filter(id=order_id)
            .first()
        )

        if order is None:
            raise ValueError(_("未找到所选医院订单。"))

        if not order.factory_id:
            raise ValueError(
                _("该医院订单尚未关联工厂，请先在医院订单中确认工厂信息。")
            )

    factory = order.factory if order and order.factory_id else None

    selection_mode = confirmation_type or "auto"
    if selection_mode not in {"auto", *FactoryConfirmation.ConfirmationType.values}:
        raise ValueError(_("无效的发货类型。"))
    confirmation = FactoryConfirmation.objects.create(
        confirmation_type=(
            FactoryConfirmation.ConfirmationType.INITIAL
            if selection_mode == "auto" else selection_mode
        ),
        extracted_confirmation_data={"django": {"batch_selection_mode": selection_mode}},
        factory=factory,
        order=order,
        confirmation_pdf=uploaded_file,
        created_by=user,
        extraction_status=FactoryConfirmation.ExtractionStatus.NOT_STARTED,
        bon_de_commande_manual_confirmed=bool(order),
        bon_de_commande_manual_note="",
        bon_de_commande_manual_confirmed_by=(
            user if order else None
        ),
        bon_de_commande_manual_confirmed_at=(
            timezone.now() if order else None
        ),
    )

    try:
        extract_factory_confirmation_for_confirmation(confirmation)
        confirmation.refresh_from_db()

        django_data = (confirmation.extracted_confirmation_data or {}).get("django") or {}
        workflow_item_id = django_data.get("workflow_item_id")
        workflow_validation_result = django_data.get("workflow_validation_result") or {}
        requires_manual_confirmation = django_data.get("requires_manual_confirmation")

        errors = workflow_validation_result.get("errors") or []
        warnings = workflow_validation_result.get("warnings") or []

        if requires_manual_confirmation:
            return (
                confirmation,
                False,
                _("工厂采购文件已提取，但识别编号与已选择订单不一致。请人工确认后继续。"),
            )

        if confirmation.order_id and not workflow_item_id:
            return (
                confirmation,
                False,
                _("工厂采购文件已提取，但尚未完成后续处理。请在详情页继续。"),
            )

        if errors:
            return (
                confirmation,
                False,
                _('已上传并进入工作流，但验证发现 %(value1)s 个问题。请查看详情页或工作流页面。') % {'value1': len(errors)},
            )

        if warnings:
            return (
                confirmation,
                True,
                _('已上传并进入工作流，但有 %(value1)s 个提醒需要检查。') % {'value1': len(warnings)},
            )

        if workflow_item_id:
            return (
                confirmation,
                True,
                _("已上传、自动提取并进入工作流，验证通过。"),
            )

        return (
            confirmation,
            False,
            _("已上传并提取，但没有创建 WorkflowItem。请检查 ShipmentBatch / workflow sync。"),
        )

    except Exception as exc:
        confirmation.refresh_from_db()
        error_text = confirmation.extraction_error or str(exc)

        if (
            confirmation.extraction_status
            == FactoryConfirmation.ExtractionStatus.NOT_STARTED
        ):
            confirmation.extraction_status = (
                FactoryConfirmation.ExtractionStatus.FAILED
            )
            confirmation.extraction_error = error_text
            confirmation.save(
                update_fields=[
                    "extraction_status",
                    "extraction_error",
                    "updated_at",
                ]
            )

        return confirmation, False, error_text


def associate_order_and_finalize_factory_confirmation(
    *,
    confirmation_id,
    order_id,
    user=None,
    confirmation_type=None,
):
    confirmation = (
        FactoryConfirmation.objects
        .select_related("order", "factory")
        .get(id=confirmation_id)
    )

    if confirmation_type:
        if confirmation_type not in FactoryConfirmation.ConfirmationType.values:
            raise ValueError(_("请选择有效的人工发货类型。"))
        data = confirmation.extracted_confirmation_data or {}
        metadata = data.setdefault("django", {})
        metadata["batch_selection_mode"] = confirmation_type
        metadata["batch_selected_by_user_id"] = getattr(user, "pk", None)
        confirmation.confirmation_type = confirmation_type
        confirmation.extracted_confirmation_data = data
        confirmation.save(update_fields=["confirmation_type", "extracted_confirmation_data", "updated_at"])

    existing_batch = get_shipment_batch(confirmation)

    if (
        existing_batch
        and confirmation.order_id
        and str(confirmation.order_id) != str(order_id)
    ):
        raise ValueError(
            _("这份工厂文件已经对应发货批次，不能直接改绑到其他医院订单，需要人工检查。")
        )

    order = Order.objects.get(id=order_id)
    if not order.factory_id:
        raise ValueError(
            _("该医院订单尚未关联工厂，请先在医院订单中确认工厂信息。")
        )

    confirmation.order = order
    confirmation.factory = order.factory
    confirmation.bon_de_commande_manual_confirmed = True
    confirmation.bon_de_commande_manual_note = ""
    confirmation.bon_de_commande_manual_confirmed_by = user
    confirmation.bon_de_commande_manual_confirmed_at = timezone.now()
    confirmation.save(
        update_fields=[
            "order",
            "factory",
            "bon_de_commande_manual_confirmed",
            "bon_de_commande_manual_note",
            "bon_de_commande_manual_confirmed_by",
            "bon_de_commande_manual_confirmed_at",
            "updated_at",
        ]
    )

    return finalize_factory_confirmation_after_order_match(
        confirmation=confirmation,
        user=user,
    )

def reextract_factory_confirmation_for_portal(confirmation_id):
    """
    Portal 详情页里的“重新提取”。

    重新调用底层 extraction service。
    底层 service 会：
      - 重新提取 PDF
      - 重新识别 bon de commande
      - 重新创建 SerialItem
      - 同步 ShipmentBatch
      - 重新计算 confirmed/backordered
    """
    confirmation = FactoryConfirmation.objects.get(id=confirmation_id)

    extract_factory_confirmation_for_confirmation(confirmation)

    confirmation.refresh_from_db()

    return confirmation


@transaction.atomic
def safely_delete_factory_confirmation_for_portal(confirmation_id):
    """
    安全删除一份 FactoryConfirmation。

    删除顺序：
      1. 如果已经生成 Invoice / PO，则阻止删除
      2. 删除 DocumentWorkflowItem
      3. 删除 ShipmentBatchItem
      4. 删除 ShipmentBatch
      5. 删除 SerialItem
      6. 删除 FactoryConfirmation
      7. 重新计算 OrderItem confirmed/backordered
      8. 同步 backorder
    """
    confirmation = (
        FactoryConfirmation.objects
        .select_related("order")
        .get(id=confirmation_id)
    )

    order = confirmation.order if confirmation.order_id else None
    order_number = (
        order.bon_de_commande
        if order
        else _("未匹配订单")
    )

    batches = list(
        ShipmentBatch.objects
        .filter(factory_confirmation=confirmation)
    )

    batch_ids = [batch.id for batch in batches]

    if batch_ids:
        generated_workflow_exists = (
            DocumentWorkflowItem.objects
            .filter(shipment_batch_id__in=batch_ids)
            .filter(
                Q(invoice_document__isnull=False)
                | Q(po_document__isnull=False)
            )
            .exists()
        )

        if generated_workflow_exists:
            raise ValueError(
                _("这份工厂确认已经生成过 Invoice 或 PO。"
                "为了避免文件和发货数据不一致，请先人工检查相关 workflow / documents，"
                "暂时不允许直接删除。")
            )

    workflow_deleted = 0
    shipment_item_deleted = 0
    shipment_batch_deleted = 0

    if batch_ids:
        workflow_deleted = (
            DocumentWorkflowItem.objects
            .filter(shipment_batch_id__in=batch_ids)
            .delete()[0]
        )

        shipment_item_deleted = (
            ShipmentBatchItem.objects
            .filter(batch_id__in=batch_ids)
            .delete()[0]
        )

        shipment_batch_deleted = (
            ShipmentBatch.objects
            .filter(id__in=batch_ids)
            .delete()[0]
        )

    serial_deleted = confirmation.serial_items.all().delete()[0]

    pdf_name = ""
    if confirmation.confirmation_pdf:
        pdf_name = confirmation.confirmation_pdf.name

    confirmation_id_before_delete = confirmation.id
    confirmation.delete()

    if order:
        warnings = []
        recalculate_order_items_from_shipment_batches(
            order=order,
            warnings=warnings,
        )

        if sync_backorders_for_order:
            sync_backorders_for_order(order)

    return {
        "confirmation_id": confirmation_id_before_delete,
        "order_number": order_number,
        "pdf_name": pdf_name,
        "workflow_deleted": workflow_deleted,
        "shipment_item_deleted": shipment_item_deleted,
        "shipment_batch_deleted": shipment_batch_deleted,
        "serial_deleted": serial_deleted,
    }

def factory_combined_status(confirmation):
    """
    工厂采购列表页的单一状态。
    合并 extraction_status + order match。
    """
    if confirmation.extraction_status == FactoryConfirmation.ExtractionStatus.FAILED:
        return _("提取失败"), "danger"

    if confirmation.extraction_status != FactoryConfirmation.ExtractionStatus.SUCCESS:
        return _("待提取"), "warning"

    if not confirmation.order_id:
        return _("待匹配"), "warning"

    return _("已就绪"), "success"


def parse_html_date(value):
    """
    解析 HTML date input 返回的 YYYY-MM-DD。
    """
    if not value:
        return None

    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def save_factory_serial_manual_edit(*, confirmation_id, post_data, user=None):
    """
    保存工厂采购详情页中的 SerialItem 人工修改。

    注意：
    不能只保存 SerialItem。
    因为 serial 修改后会影响：
        1. ShipmentBatch / ShipmentBatchItem
        2. OrderItem.confirmed_quantity / backordered_quantity
        3. Backorder
        4. WorkflowItem
        5. Workflow validation

    所以保存后必须重新同步整条后续流程。
    """
    confirmation = (
        FactoryConfirmation.objects
        .select_related("order", "factory")
        .get(id=confirmation_id)
    )

    errors = []
    warnings = []

    if not confirmation.order_id:
        errors.append(_("这份工厂采购文件还没有匹配医院订单，不能保存 serial 修改。"))
        return errors, warnings, None

    serial_items = list(
        confirmation.serial_items
        .select_related("product")
        .order_by("id")
    )

    seen_serial_numbers = set()

    for serial in serial_items:
        product_code = str(
            post_data.get(f"serial_{serial.id}_product_code") or ""
        ).strip()

        serial_number = str(
            post_data.get(f"serial_{serial.id}_serial_number") or ""
        ).strip()

        expiration_date = parse_html_date(
            post_data.get(f"serial_{serial.id}_expiration_date")
        )

        if not product_code:
            errors.append(_('SerialItem #%(value1)s: 产品号不能为空。') % {'value1': serial.id})
            continue

        if not serial_number:
            errors.append(_('SerialItem #%(value1)s: Serial Number 不能为空。') % {'value1': serial.id})
            continue

        if serial_number in seen_serial_numbers:
            errors.append(
                _('Serial Number %(value1)s 在当前文件中重复。') % {'value1': serial_number}
            )
            continue

        seen_serial_numbers.add(serial_number)

        existing_serial = (
            SerialItem.objects
            .filter(serial_number=serial_number)
            .exclude(id=serial.id)
            .select_related("factory_confirmation", "order")
            .first()
        )

        if existing_serial:
            errors.append(
                _('Serial Number %(value1)s 已经存在于 FactoryConfirmation #%(value2)s，不能重复使用。') % {'value1': serial_number, 'value2': existing_serial.factory_confirmation_id}
            )
            continue

        product = Product.objects.filter(code=product_code).first()

        if not product:
            warnings.append(
                _('产品 %(value1)s 不在产品库中，已保存但需要人工检查。') % {'value1': product_code}
            )

        raw_data = serial.raw_data or {}
        raw_data.setdefault("manual_edits", [])
        raw_data["manual_edits"].append({
            "edited_by": getattr(user, "id", None),
            "product_code_before": serial.product_code,
            "serial_number_before": serial.serial_number,
            "expiration_date_before": (
                serial.expiration_date.isoformat()
                if serial.expiration_date
                else None
            ),
            "product_code_after": product_code,
            "serial_number_after": serial_number,
            "expiration_date_after": (
                expiration_date.isoformat()
                if expiration_date
                else None
            ),
        })

        serial.product_code = product_code
        serial.serial_number = serial_number
        serial.expiration_date = expiration_date
        serial.product = product
        serial.discount_rate = calculate_preliminary_discount_rate(
            expiration_date
        )
        serial.raw_data = raw_data

        serial.save(
            update_fields=[
                "product_code",
                "serial_number",
                "expiration_date",
                "product",
                "discount_rate",
                "raw_data",
            ]
        )

    if errors:
        return errors, warnings, None

    # 保存 SerialItem 后，重新同步发货批次。
    shipment_sync_result = sync_shipment_batch_from_factory_confirmation(
        confirmation
    )

    # 有些 service 可能返回 batch，也可能返回 (batch, created)。
    if isinstance(shipment_sync_result, tuple):
        shipment_batch = shipment_sync_result[0]
    else:
        shipment_batch = shipment_sync_result

    if not shipment_batch:
        errors.append(_("Serial 修改已保存，但同步 ShipmentBatch 失败。"))
        return errors, warnings, None

    # 重新计算 OrderItem 的 confirmed/backordered。
    recalculate_order_items_from_shipment_batches(
        order=confirmation.order,
        warnings=warnings,
    )

    # 同步 backorder。
    if sync_backorders_for_order:
        sync_backorders_for_order(confirmation.order)

    # 重新同步 workflow。
    workflow_sync_result = sync_document_workflow_item_for_batch(
        shipment_batch
    )

    workflow_item = get_workflow_item_from_sync_result(
        workflow_sync_result
    )

    if not workflow_item:
        errors.append(_("Serial 修改已保存，但同步 WorkflowItem 失败。"))
        return errors, warnings, None

    validation_result = validate_document_workflow_item(
        workflow_item,
        save=True,
    )

    workflow_item.refresh_from_db()

    # 把人工修改和重新验证结果写回 extracted_confirmation_data，方便追踪。
    extracted_data = confirmation.extracted_confirmation_data or {}
    django_data = extracted_data.setdefault("django", {})

    django_data["manual_serial_edit_at"] = datetime.now().isoformat()
    django_data["manual_serial_edit_by"] = getattr(user, "id", None)
    django_data["shipment_batch_id"] = shipment_batch.id
    django_data["workflow_item_id"] = workflow_item.id
    django_data["workflow_status"] = workflow_item.workflow_status
    django_data["workflow_validation_status"] = workflow_item.validation_status
    django_data["workflow_validation_result"] = (
        workflow_item.validation_data or validation_result
    )

    confirmation.extracted_confirmation_data = extracted_data
    confirmation.save(
        update_fields=[
            "extracted_confirmation_data",
            "updated_at",
        ]
    )

    return errors, warnings, workflow_item
