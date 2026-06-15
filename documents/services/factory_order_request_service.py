# -*- coding: utf-8 -*-

import json
import re
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from documents.models import GeneratedDocument
from documents.services.document_numbering_service import parse_document_date

def sanitize_filename(text):
    text = str(text).strip()
    text = re.sub(r"[^\w\-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def format_quantity(value):
    if value is None:
        return ""

    value = float(value)

    if value.is_integer():
        return str(int(value))

    return f"{value:.2f}"


def get_order_date_from_order(order):
    """
    从医院订单提取结果中读取 Date de commande。
    优先使用 order.extracted_order_data["header"]["order_date"]。
    """
    data = getattr(order, "extracted_order_data", None) or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    order_date = (
        data.get("header", {}).get("order_date")
        or data.get("summary", {}).get("order_date")
        or ""
    )

    return str(order_date).strip()


def format_date_display(d):
    return d.strftime("%d/%m/%Y")


def resolve_order_request_date(order, manual_date=None):
    """
    Factory Order Request 的业务日期规则：

    1. 手动日期优先
    2. 否则用医院订单提取日期
    3. 再没有才用今天日期
    """
    warnings = []

    if manual_date:
        parsed = parse_document_date(manual_date)
        return format_date_display(parsed), "manual_document_date", warnings

    raw_order_date = get_order_date_from_order(order)

    if raw_order_date:
        parsed = None

        try:
            parsed = parse_document_date(raw_order_date)
        except Exception:
            parsed = None

        if parsed:
            return format_date_display(parsed), "hospital_order_date", warnings

        return raw_order_date, "hospital_order_date_raw", warnings

    today = timezone.localdate()
    warnings.append(
        f"Order {order.bon_de_commande}: hospital order date is missing. "
        f"Fallback to today's date {today.isoformat()}."
    )

    return format_date_display(today), "fallback_today", warnings


def get_order_items_queryset(order):
    """
    兼容不同 related_name。
    如果你的 OrderItem FK related_name 是 items，则用 order.items。
    如果没有 related_name，则用 order.orderitem_set。
    """
    if hasattr(order, "items"):
        return order.items.all()

    return order.orderitem_set.all()


def load_company_info():
    """
    读取 config/company_info.json，并整理成和 PO 模板相同的数据结构。
    """
    project_root = Path(settings.BASE_DIR)
    path = project_root / "config" / "company_info.json"

    fallback = {
        "logo_path": "data/logo.png",
        "company_name": "Dela Global Trade Consulting Ltd",
        "registration_no": "71 99 26 22",
        "po_company": {
            "display_name": "DELA GLOBAL HK",
            "address": [
                "R1009,10/F, Front Block, Ming Sang Industrial Building",
                "19 Hing Yip Street Kwun Tong, KL",
                "Hong Kong",
            ],
        },
    }

    if not path.exists():
        return fallback

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    po_company = data.get("po_company") or {}

    return {
        "logo_path": data.get("logo_path") or fallback["logo_path"],
        "company_name": data.get("company_name") or fallback["company_name"],
        "registration_no": data.get("registration_no") or fallback["registration_no"],
        "po_company": {
            "display_name": po_company.get("display_name") or fallback["po_company"]["display_name"],
            "address": po_company.get("address") or fallback["po_company"]["address"],
        },
    }


def build_factory_data(order):
    """
    从 Order.factory 读取工厂信息。
    输出结构保持和 PO 模板一致。
    """
    factory = getattr(order, "factory", None)

    if factory is None:
        raise ValueError(
            f"订单 {order.bon_de_commande} 没有关联 factory，不能生成 Factory Order Request。"
        )

    address_raw = getattr(factory, "address", "") or ""

    address_lines = [
        line.strip()
        for line in str(address_raw).splitlines()
        if line.strip()
    ]

    if not address_lines and address_raw:
        address_lines = [
            part.strip()
            for part in str(address_raw).split(",")
            if part.strip()
        ]

    return {
        "factory_name": (
            getattr(factory, "legal_name", None)
            or getattr(factory, "name", None)
            or getattr(factory, "short_name", None)
            or str(factory)
        ),
        "factory_address": address_lines,
    }

def build_items(order):
    """
    从医院订单提取出的 OrderItem 生成产品行。
    不使用 FactoryConfirmation。
    """
    items = []

    qs = get_order_items_queryset(order).select_related("product")

    for order_item in qs:
        product = getattr(order_item, "product", None)

        product_code = (
            getattr(product, "code", None)
            or getattr(order_item, "product_code", None)
            or ""
        )

        description = (
            getattr(product, "description", None)
            or getattr(order_item, "description", None)
            or ""
        )

        quantity = (
            getattr(order_item, "requested_quantity", None)
            or getattr(order_item, "final_quantity", None)
            or getattr(order_item, "confirmed_quantity", None)
            or 0
        )

        if not product_code:
            continue

        items.append({
            "product_code": product_code,
            "description": description,
            "quantity_raw": float(quantity or 0),
            "quantity": format_quantity(quantity),
        })

    if not items:
        raise ValueError(
            f"订单 {order.bon_de_commande} 没有可生成的产品行。"
        )

    return items


def render_html(data):
    project_root = Path(settings.BASE_DIR)
    template_dir = project_root / "templates"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    template = env.get_template("factory_order_request.html")

    return template.render(**data)


def generate_factory_order_request(order, generated_by=None, document_date=None):    
    """
    生成 Factory Order Request。

    注意：
    这个文件只依赖医院订单提取结果。
    不依赖工厂确认文件。
    """
    bon = str(order.bon_de_commande).strip()

    document_number = f"Factory Order Request {bon}"

    items = build_items(order)
    total_units_raw = sum(float(item.get("quantity_raw") or 0) for item in items)

    order_date, date_source, date_warnings = resolve_order_request_date(
        order=order,
        manual_date=document_date,
    )

    data = {
        "document": {
            "document_number": document_number,
            "bon_de_commande": bon,
            "order_date": order_date,
            "order_date_source": date_source,
            "generated_at": timezone.now().isoformat(),
        },
        "company": load_company_info(),
        "factory": build_factory_data(order),
        "items": items,
        "totals": {
            "total_units_raw": total_units_raw,
            "total_units": format_quantity(total_units_raw),
        },
        "debug": {
            "source": "hospital_order_only",
            "uses_factory_confirmation": False,
            "order_id": order.id,
            "business_date_source": date_source,
        },
        "warnings": date_warnings,
    }

    html_content = render_html(data)

    project_root = Path(settings.BASE_DIR)
    base_url = project_root.resolve().as_uri() + "/"

    pdf_bytes = HTML(
        string=html_content,
        base_url=base_url,
    ).write_pdf()

    safe_name = sanitize_filename(document_number)

    document, _ = GeneratedDocument.objects.update_or_create(
        order=order,
        document_type="factory_order_request",
        defaults={
            "document_number": document_number,
            "source_data": data,
            "generated_by": generated_by,
            "notes": "Generated from hospital order only. No factory confirmation used.",
        },
    )

    document.html_file.save(
        f"{safe_name}.html",
        ContentFile(html_content.encode("utf-8")),
        save=False,
    )

    document.pdf_file.save(
        f"{safe_name}.pdf",
        ContentFile(pdf_bytes),
        save=False,
    )

    document.save()

    return document
