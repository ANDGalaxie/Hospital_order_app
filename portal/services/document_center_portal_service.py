from portal.i18n import display_choice, display_choices
from django.utils.translation import gettext as _, gettext_lazy
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from documents.models import GeneratedDocument

LEGACY_DOCUMENT_TYPES = ("hospital_invoice", "factory_po", "factory_order_request")
LEGACY_DOCUMENT_CHOICES = [choice for choice in GeneratedDocument.DocumentType.choices if choice[0] in LEGACY_DOCUMENT_TYPES]


DOCUMENT_TYPE_LABELS = {
    GeneratedDocument.DocumentType.HOSPITAL_INVOICE: (
        "Hospital Invoice"
    ),
    GeneratedDocument.DocumentType.FACTORY_PO: (
        "Factory Purchase Order"
    ),
    GeneratedDocument.DocumentType.FACTORY_ORDER_REQUEST: (
        "Factory Order Request"
    ),
}


DOCUMENT_TYPE_CLASSES = {
    GeneratedDocument.DocumentType.HOSPITAL_INVOICE: (
        "invoice"
    ),
    GeneratedDocument.DocumentType.FACTORY_PO: (
        "po"
    ),
    GeneratedDocument.DocumentType.FACTORY_ORDER_REQUEST: (
        "request"
    ),
}


def parse_iso_date(value):
    value = str(value or "").strip()

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return None


def parse_month_key(value):
    value = str(value or "").strip()

    if not re.fullmatch(r"\d{4}-\d{2}", value):
        return None

    try:
        year, month = value.split("-")

        return int(year), int(month)
    except (TypeError, ValueError):
        return None


def decimal_from_value(value):
    if value is None or value == "":
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value)
    text = text.replace("\u00a0", " ")
    text = text.replace(",", "")
    text = re.sub(
        r"[^0-9.\-]",
        "",
        text,
    )

    if not text:
        return None

    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def get_document_payload(document):
    """
    获取真正的业务文档数据。

    当前生成结构：
    - Invoice: source_data["invoice_data"]
    - PO: source_data["po_data"]
    - Factory Request: 兼容多个历史字段名

    同时兼容旧文档直接把 totals/items
    保存在 source_data 顶层的情况。
    """
    data = document.source_data or {}

    if not isinstance(data, dict):
        return {}

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        payload = data.get("invoice_data")

        if isinstance(payload, dict):
            return payload

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_PO
    ):
        payload = data.get("po_data")

        if isinstance(payload, dict):
            return payload

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_ORDER_REQUEST
    ):
        for key in [
            "factory_order_request_data",
            "factory_request_data",
            "request_data",
            "order_request_data",
        ]:
            payload = data.get(key)

            if isinstance(payload, dict):
                return payload

    return data


def extract_document_amount(document):
    """
    从真正的文档 payload 中读取金额。

    Hospital Invoice:
        invoice_data["totals"]["total_raw"]

    Factory PO:
        po_data["totals"]["total_raw"]

    Factory Request:
        通常没有最终金额，返回 None。
    """
    payload = get_document_payload(document)

    totals = payload.get("totals") or {}

    if not isinstance(totals, dict):
        totals = {}

    candidate_keys = [
        "total_raw",
        "total_amount_raw",
        "actual_factory_total",
        "grand_total_raw",
        "untaxed_amount_raw",
        "total",
        "total_amount",
        "amount",
    ]

    for key in candidate_keys:
        if key not in totals:
            continue

        value = decimal_from_value(
            totals.get(key)
        )

        if value is not None:
            return value

    for key in candidate_keys:
        if key not in payload:
            continue

        value = decimal_from_value(
            payload.get(key)
        )

        if value is not None:
            return value

    # Invoice 和 PO 的历史数据如果没有 totals，
    # 尝试对产品行金额求和。
    if document.document_type in {
        GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE,
        GeneratedDocument
        .DocumentType
        .FACTORY_PO,
    }:
        items = payload.get("items") or []

        if isinstance(items, list):
            total = Decimal("0.00")
            found_amount = False

            for item in items:
                if not isinstance(item, dict):
                    continue

                amount = decimal_from_value(
                    item.get("amount_raw")
                    or item.get("amount")
                )

                if amount is not None:
                    total += amount
                    found_amount = True

            if found_amount:
                return total

    return None


def extract_document_due_date(document):
    payload = get_document_payload(
        document
    )

    invoice_data = (
        payload.get("invoice")
        or {}
    )

    if not isinstance(
        invoice_data,
        dict,
    ):
        return ""

    return (
        invoice_data.get("due_date")
        or invoice_data.get(
            "payment_due_date"
        )
        or ""
    )


def extract_document_item_count(document):
    payload = get_document_payload(
        document
    )

    items = payload.get("items") or []

    if isinstance(items, list):
        return len(items)

    return 0


def extract_document_total_quantity(document):
    payload = get_document_payload(
        document
    )

    totals = payload.get("totals") or {}

    if isinstance(totals, dict):
        for key in [
            "total_units_raw",
            "total_quantity_raw",
            "total_units",
            "total_quantity",
        ]:
            if key not in totals:
                continue

            value = decimal_from_value(
                totals.get(key)
            )

            if value is not None:
                return value

    items = payload.get("items") or []

    if not isinstance(items, list):
        return None

    total = Decimal("0.00")
    found_quantity = False

    for item in items:
        if not isinstance(item, dict):
            continue

        quantity = decimal_from_value(
            item.get("quantity_raw")
            or item.get("quantity")
        )

        if quantity is not None:
            total += quantity
            found_quantity = True

    return (
        total
        if found_quantity
        else None
    )


def build_file_info(field_file):
    result = {
        "name": "",
        "url": "",
        "exists": False,
        "size": None,
        "size_label": "",
    }

    if not field_file or not field_file.name:
        return result

    result["name"] = field_file.name

    try:
        result["url"] = field_file.url
    except Exception:
        result["url"] = ""

    # Static media URLs are commonly cached by the browser/web server.
    # Include the actual file modification marker so a regenerated PDF
    # is fetched immediately without changing the storage path.
    version = None
    try:
        modified_at = field_file.storage.get_modified_time(field_file.name)
        if modified_at:
            version = int(modified_at.timestamp() * 1000000)
    except Exception:
        version = None

    if version is None:
        try:
            version = field_file.storage.size(field_file.name)
        except Exception:
            version = None

    if result["url"] and version is not None:
        separator = "&" if "?" in result["url"] else "?"
        result["url"] = f"{result['url']}{separator}v={version}"

    try:
        result["exists"] = (
            field_file.storage.exists(
                field_file.name
            )
        )
    except Exception:
        result["exists"] = False

    if result["exists"]:
        try:
            result["size"] = (
                field_file.storage.size(
                    field_file.name
                )
            )
        except Exception:
            result["size"] = None

    if result["size"] is not None:
        size = result["size"]

        if size >= 1024 * 1024:
            result["size_label"] = (
                f"{size / (1024 * 1024):.2f} MB"
            )
        elif size >= 1024:
            result["size_label"] = (
                f"{size / 1024:.1f} KB"
            )
        else:
            result["size_label"] = (
                f"{size} B"
            )

    return result


def get_hospital_label(document):
    order = document.order

    if order.hospital_id:
        return order.hospital.name

    return order.hospital_name or "—"


def get_factory_label(document):
    factory = document.order.factory

    if not factory:
        return "—"

    return (
        factory.short_name
        or factory.name
    )


def decorate_document(document):
    document.portal_type_label = (
        DOCUMENT_TYPE_LABELS.get(
            document.document_type,
            display_choice(document.get_document_type_display()),
        )
    )

    document.portal_type_class = (
        DOCUMENT_TYPE_CLASSES.get(
            document.document_type,
            "other",
        )
    )

    document.portal_hospital_label = (
        get_hospital_label(document)
    )

    document.portal_factory_label = (
        get_factory_label(document)
    )

    document.portal_amount = (
        extract_document_amount(document)
    )

    document.portal_due_date = (
        extract_document_due_date(document)
    )

    document.portal_item_count = (
        extract_document_item_count(document)
    )

    document.portal_total_quantity = (
        extract_document_total_quantity(
            document
        )
    )

    document.portal_pdf = build_file_info(
        document.pdf_file
    )

    document.portal_html = build_file_info(
        document.html_file
    )

    return document


def build_document_month_options():
    month_keys = set()

    generated_dates = (
        GeneratedDocument.objects.filter(document_type__in=LEGACY_DOCUMENT_TYPES)
        .values_list(
            "generated_at",
            flat=True,
        )
    )

    for generated_at in generated_dates:
        if generated_at:
            local_value = timezone.localtime(
                generated_at
            )

            month_keys.add(
                local_value.strftime("%Y-%m")
            )

    return sorted(
        month_keys,
        reverse=True,
    )


DOCUMENT_LIST_PAGE_META = {
    "all": {
        "title": gettext_lazy("全部文档"),
        "description": (
            gettext_lazy("查看全部 Invoice、Factory PO "
            "和 Factory Request。")
        ),
    },
    GeneratedDocument.DocumentType.HOSPITAL_INVOICE: {
        "title": "Hospital Invoice",
        "description": (
            gettext_lazy("查看发送给医院的正式销售发票。")
        ),
    },
    GeneratedDocument.DocumentType.FACTORY_PO: {
        "title": "Factory Purchase Order",
        "description": (
            gettext_lazy("查看发送给工厂的正式采购订单。")
        ),
    },
    GeneratedDocument.DocumentType.FACTORY_ORDER_REQUEST: {
        "title": "Factory Order Request",
        "description": (
            gettext_lazy("查看医院订单提取后生成的工厂需求文件。")
        ),
    },
}


def get_document_list_page_meta(
    forced_document_type,
):
    key = forced_document_type or "all"

    return DOCUMENT_LIST_PAGE_META.get(
        key,
        DOCUMENT_LIST_PAGE_META["all"],
    )


def build_document_center_home_context(
    request,
):
    """
    文档中心分类首页。
    """
    card_configs = [
        {
            "document_type": (
                GeneratedDocument
                .DocumentType
                .HOSPITAL_INVOICE
            ),
            "title": "Hospital Invoice",
            "subtitle": _("医院销售发票"),
            "description": (
                _("查看发送给医院的正式发票、"
                "金额、付款截止日期和生成快照。")
            ),
            "theme": "invoice",
            "symbol": "INV",
            "url_name": (
                "portal:document_invoices"
            ),
        },
        {
            "document_type": (
                GeneratedDocument
                .DocumentType
                .FACTORY_PO
            ),
            "title": "Factory Purchase Order",
            "subtitle": _("工厂采购订单"),
            "description": (
                _("查看发送给工厂的正式 PO、"
                "采购金额和临期折扣结果。")
            ),
            "theme": "po",
            "symbol": "PO",
            "url_name": (
                "portal:document_factory_pos"
            ),
        },
        {
            "document_type": (
                GeneratedDocument
                .DocumentType
                .FACTORY_ORDER_REQUEST
            ),
            "title": "Factory Order Request",
            "subtitle": _("工厂需求文件"),
            "description": (
                _("查看医院订单提取后生成的"
                "工厂需求文件。")
            ),
            "theme": "request",
            "symbol": "REQ",
            "url_name": (
                "portal:document_factory_requests"
            ),
        },
    ]

    documents = (
        GeneratedDocument.objects.filter(document_type__in=LEGACY_DOCUMENT_TYPES)
        .select_related(
            "order",
            "order__hospital",
            "order__factory",
            "shipment_batch",
            "generated_by",
        )
    )

    cards = []

    for config in card_configs:
        type_documents = documents.filter(
            document_type=(
                config["document_type"]
            )
        )

        latest_document = (
            type_documents
            .order_by(
                "-generated_at",
                "-id",
            )
            .first()
        )

        if latest_document:
            decorate_document(
                latest_document
            )

        cards.append(
            {
                **config,
                "url": reverse(
                    config["url_name"]
                ),
                "count": (
                    type_documents.count()
                ),
                "latest_document": (
                    latest_document
                ),
            }
        )

    return {
        "cards": cards,
        "total_count": (
            documents.count()
        ),
        "all_documents_url": reverse(
            "portal:document_list"
        ),
    }


def build_document_list_context(request, forced_document_type=None):
    query = (
        request.GET.get("q")
        or ""
    ).strip()

    if forced_document_type:
        document_type = (
            forced_document_type
        )
    else:
        document_type = (
            request.GET.get("type")
            or "all"
        ).strip()

    show_amount_column = (
        forced_document_type
        != GeneratedDocument
        .DocumentType
        .FACTORY_ORDER_REQUEST
    )

    month_key = (
        request.GET.get("month")
        or ""
    ).strip()

    date_from_text = (
        request.GET.get("date_from")
        or ""
    ).strip()

    date_to_text = (
        request.GET.get("date_to")
        or ""
    ).strip()

    file_status = (
        request.GET.get("file_status")
        or "all"
    ).strip()

    documents = (
        GeneratedDocument.objects.filter(document_type__in=LEGACY_DOCUMENT_TYPES)
        .select_related(
            "order",
            "order__hospital",
            "order__factory",
            "shipment_batch",
            "generated_by",
        )
        .all()
    )

    if query:
        documents = documents.filter(
            Q(
                document_number__icontains=query
            )
            | Q(
                order__bon_de_commande__icontains=query
            )
            | Q(
                order__hospital_name__icontains=query
            )
            | Q(
                order__hospital__name__icontains=query
            )
            | Q(
                order__factory__name__icontains=query
            )
            | Q(
                order__factory__short_name__icontains=query
            )
            | Q(notes__icontains=query)
        )

    valid_types = {
        value
        for value, label
        in display_choices(LEGACY_DOCUMENT_CHOICES)
    }

    if document_type in valid_types:
        documents = documents.filter(
            document_type=document_type
        )

    parsed_month = parse_month_key(
        month_key
    )

    if parsed_month:
        year, month = parsed_month

        documents = documents.filter(
            generated_at__year=year,
            generated_at__month=month,
        )

    date_from = parse_iso_date(
        date_from_text
    )

    date_to = parse_iso_date(
        date_to_text
    )

    if date_from:
        documents = documents.filter(
            generated_at__date__gte=date_from
        )

    if date_to:
        documents = documents.filter(
            generated_at__date__lte=date_to
        )

    if file_status == "has_pdf":
        documents = documents.exclude(
            pdf_file=""
        )

    elif file_status == "missing_pdf":
        documents = documents.filter(
            Q(pdf_file="")
            | Q(pdf_file__isnull=True)
        )

    documents = documents.order_by(
        "-generated_at",
        "-id",
    )

    paginator = Paginator(
        documents,
        25,
    )

    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    for document in page_obj.object_list:
        decorate_document(document)

    all_documents = (
        GeneratedDocument.objects.filter(document_type__in=LEGACY_DOCUMENT_TYPES).all()
    )

    today = timezone.localdate()

    query_params = request.GET.copy()
    query_params.pop("page", None)

    page_meta = (
        get_document_list_page_meta(
            forced_document_type
        )
    )

    return {
        "page_title": page_meta["title"],
        "page_description": (
            page_meta["description"]
        ),
        "fixed_document_type": (
            forced_document_type or ""
        ),
        "document_home_url": reverse(
            "portal:document_center"
        ),
        "show_amount_column": (
            show_amount_column
        ),
        "page_obj": page_obj,
        "query": query,
        "document_type": document_type,
        "month_key": month_key,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "file_status": file_status,
        "query_without_page": (
            query_params.urlencode()
        ),
        "month_options": (
            build_document_month_options()
        ),
        "document_type_choices": (
            display_choices(LEGACY_DOCUMENT_CHOICES)
        ),
        "total_count": (
            all_documents.count()
        ),
        "invoice_count": (
            all_documents.filter(
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .HOSPITAL_INVOICE
                )
            ).count()
        ),
        "po_count": (
            all_documents.filter(
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_PO
                )
            ).count()
        ),
        "request_count": (
            all_documents.filter(
                document_type=(
                    GeneratedDocument
                    .DocumentType
                    .FACTORY_ORDER_REQUEST
                )
            ).count()
        ),
        "this_month_count": (
            all_documents.filter(
                generated_at__year=today.year,
                generated_at__month=today.month,
            ).count()
        ),
    }


def build_document_detail_context(
    request,
    document_id,
):
    document = get_object_or_404(
        GeneratedDocument.objects.filter(document_type__in=LEGACY_DOCUMENT_TYPES)
        .select_related(
            "order",
            "order__hospital",
            "order__factory",
            "shipment_batch",
            "shipment_batch__factory_confirmation",
            "generated_by",
        ),
        id=document_id,
    )

    decorate_document(document)

    workflow_item = None

    try:
        from workflow.models import (
            DocumentWorkflowItem,
        )

        workflow_item = (
            DocumentWorkflowItem.objects
            .filter(
                Q(invoice_document=document)
                | Q(po_document=document)
            )
            .order_by("-id")
            .first()
        )
    except Exception:
        workflow_item = None

    raw_source_data = (
        document.source_data
        if isinstance(
            document.source_data,
            dict,
        )
        else {}
    )

    payload_data = (
        get_document_payload(
            document
        )
    )

    source_json = json.dumps(
        raw_source_data,
        ensure_ascii=False,
        indent=2,
        default=str,
    )

    order_url = reverse(
        "portal:order_detail",
        args=[document.order_id],
    )

    workflow_url = ""

    if workflow_item:
        workflow_url = reverse(
            "portal:workflow_detail",
            args=[workflow_item.id],
        )

    confirmation_url = ""

    if (
        document.shipment_batch_id
        and document.shipment_batch
        .factory_confirmation_id
    ):
        confirmation_url = reverse(
            "portal:factory_detail",
            args=[
                document.shipment_batch
                .factory_confirmation_id
            ],
        )

    return {
        "document": document,
        "source_data": payload_data,
        "source_json": source_json,
        "source_items": (
            payload_data.get("items")
            if isinstance(
                payload_data.get("items"),
                list,
            )
            else []
        ),
        "source_serial_items": (
            payload_data.get("serial_items")
            if isinstance(
                payload_data.get(
                    "serial_items"
                ),
                list,
            )
            else []
        ),
        "order_url": order_url,
        "workflow_item": workflow_item,
        "workflow_url": workflow_url,
        "confirmation_url": (
            confirmation_url
        ),
    }
