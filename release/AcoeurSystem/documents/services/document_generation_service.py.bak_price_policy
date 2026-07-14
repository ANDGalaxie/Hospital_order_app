import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.models import GeneratedDocument
from documents.services.document_numbering_service import (
    get_or_create_document_numbers,
    parse_document_date,
)
from factory_confirmations.models import FactoryConfirmation, SerialItem
from orders.models import Order, OrderItem
from orders.services.order_validation_service import (
    validate_order_for_document_generation,
)

from legacy_services.hospital_invoice_generator import (
    render_invoice_html,
    write_html_and_pdf as write_invoice_html_and_pdf,
    format_eur,
    format_quantity,
    format_serial_quantity,
    format_date_fr,
)

from legacy_services.factory_po_generator import (
    render_po_html,
    write_html_and_pdf as write_po_html_and_pdf,
)


EXPIRATION_THRESHOLD_DAYS = 365
EXPIRATION_DISCOUNT_RATE = Decimal("0.30")
DEFAULT_FACTORY_UNIT_PRICE = Decimal("120.00")
EXPECTED_ARRIVAL_DAYS = 3

def prepare_company_info(company_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    整理 company_info。

    PO 模板需要 company.po_company。
    如果 config/company_info.json 没有 po_company，这里自动补默认香港公司信息。
    """
    data = dict(company_info or {})

    if "po_company" not in data:
        data["po_company"] = {
            "display_name": "DELA GLOBAL HK",
            "address": [
                "R1009,10/F, Front Block, Ming Sang Industrial Building",
                "19 Hing Yip Street Kwun Tong, KL",
                "Hong Kong",
            ],
        }

    if "logo_path" not in data:
        data["logo_path"] = "data/logo.png"

    if "company_name" not in data:
        data["company_name"] = "Dela Global Trade Consulting Limited"

    if "registration_no" not in data:
        data["registration_no"] = "71 99 26 22"

    return data


def prepare_factory_info(factory_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Django 版本中暂时直接使用 factory_info.json 的内容。
    同时给常用字段提供 fallback，避免模板缺字段时报错。
    """
    data = dict(factory_info or {})

    data.setdefault("factory_name", data.get("name", ""))
    data.setdefault("factory_address", data.get("address", ""))
    data.setdefault("buyer", data.get("buyer_name", ""))
    data.setdefault(
        "default_product_description",
        "HT-Supreme™ Drug Eluting Stent",
    )

    return data


def build_factory_info_from_model(factory) -> Dict[str, Any]:
    """
    从 Factory 模型构造 PO 模板需要的 factory_info。
    """
    if factory is None:
        raise ValueError("Factory is missing. Cannot generate Factory PO.")

    address_lines = split_text_lines(factory.address)

    return {
        "name": factory.name,
        "display_name": factory.short_name or factory.name,
        "factory_name": factory.name,
        "legal_name": factory.legal_name,
        "address": address_lines,
        "factory_address": address_lines,
        "buyer": factory.buyer,
        "default_product_description": (
            factory.default_product_description
            or "HT-Supreme™ Drug Eluting Stent"
        ),
    }


def format_date_display(d) -> str:
    """
    PO 中日期显示为 dd/mm/yyyy。
    """
    return d.strftime("%d/%m/%Y")


def try_parse_business_date(value):
    """
    尝试把任意日期值转成 date。
    支持:
        date
        datetime
        YYYY-MM-DD
        DD/MM/YYYY
        DD-MM-YYYY
    """
    if not value:
        return None

    try:
        return parse_document_date(value)
    except Exception:
        return None


def get_hospital_order_date(order: Order):
    """
    从医院订单提取结果中读取 Date de commande。

    优先级：
        1. order.extracted_order_data["header"]["order_date"]
        2. order.extracted_order_data["summary"]["order_date"]
    """
    data = getattr(order, "extracted_order_data", None) or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    raw_date = (
        data.get("header", {}).get("order_date")
        or data.get("summary", {}).get("order_date")
    )

    parsed = try_parse_business_date(raw_date)

    if parsed:
        return parsed, "hospital_order_date"

    return None, ""


def get_factory_shipping_date(confirmation: FactoryConfirmation):
    """
    优先从 confirmation.shipping_date 读取工厂发货日期。
    如果没有，再从 extracted_confirmation_data 里读取。
    """
    if confirmation and confirmation.shipping_date:
        return confirmation.shipping_date, "factory_confirmation.shipping_date"

    data = getattr(confirmation, "extracted_confirmation_data", None) or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    factory_document = data.get("factory_document", {}) or {}

    raw_date = (
        factory_document.get("shipping_date_only_iso")
        or factory_document.get("shipping_date")
        or factory_document.get("date")
    )

    parsed = try_parse_business_date(raw_date)

    if parsed:
        return parsed, "factory_confirmation.extracted_confirmation_data"

    return None, ""


def resolve_invoice_po_document_date(
    order: Order,
    confirmation: FactoryConfirmation,
    manual_date: Optional[Any] = None,
):
    """
    Invoice / Factory PO 的业务日期规则：

    1. 手动日期优先
    2. 否则使用工厂确认文件 shipping_date
    3. 再没有才使用今天日期，并给 warning
    """
    warnings = []

    if manual_date:
        parsed = parse_document_date(manual_date)
        return parsed, "manual_document_date", warnings

    shipping_date, source = get_factory_shipping_date(confirmation)

    if shipping_date:
        return shipping_date, source, warnings

    fallback_date = timezone.localdate()
    warnings.append(
        f"Order {order.bon_de_commande}: factory shipping_date is missing. "
        f"Fallback to today's date {fallback_date.isoformat()}."
    )

    return fallback_date, "fallback_today", warnings


def resolve_factory_po_document_date(
    order: Order,
    confirmation: FactoryConfirmation,
    manual_date: Optional[Any] = None,
):
    """
    Factory PO 的业务日期规则：

    1. 手动日期优先
    2. 否则使用医院订单 Date de commande
    3. 如果医院订单日期缺失，再 fallback 到工厂确认 shipping_date
    4. 如果 shipping_date 也缺失，再用今天，并给 warning
    """
    warnings = []

    if manual_date:
        parsed = parse_document_date(manual_date)
        return parsed, "manual_document_date", warnings

    hospital_order_date, source = get_hospital_order_date(order)

    if hospital_order_date:
        return hospital_order_date, source, warnings

    shipping_date, shipping_source = get_factory_shipping_date(confirmation)

    if shipping_date:
        warnings.append(
            f"Order {order.bon_de_commande}: hospital order date is missing. "
            f"Factory PO uses factory shipping_date {shipping_date.isoformat()} instead."
        )
        return shipping_date, shipping_source, warnings

    fallback_date = timezone.localdate()
    warnings.append(
        f"Order {order.bon_de_commande}: hospital order date and factory shipping_date "
        f"are both missing. Factory PO falls back to today's date "
        f"{fallback_date.isoformat()}."
    )

    return fallback_date, "fallback_today", warnings


def format_po_quantity(value: float) -> str:
    return f"{float(value):.2f}"


def format_po_unit_price(value: float) -> str:
    return f"{float(value):.2f}"


def format_po_discount(rate: float) -> str:
    return f"{float(rate) * 100:.2f}%"


def format_po_eur(value: float) -> str:
    return f"{float(value):,.2f} €"

def load_json_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def sanitize_filename(text: str) -> str:
    text = str(text).strip()
    text = re.sub(r"[^\w\-.]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_") or "document"


def json_safe(value: Any) -> Any:
    """
    把 Decimal / date / datetime 等对象转成 JSONField 可以保存的格式。
    """
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime,)):
        return value.isoformat()

    if hasattr(value, "isoformat"):
        return value.isoformat()

    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}

    if isinstance(value, list):
        return [json_safe(v) for v in value]

    return value


def save_json_file(data: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(json_safe(data), f, ensure_ascii=False, indent=2)


def get_order_document_workspace(order: Order) -> Path:
    return (
        Path(settings.MEDIA_ROOT)
        / "order_workspaces"
        / f"order_{order.id}"
        / "documents"
    )


def media_relative_path(path: Path) -> str:
    return str(path.relative_to(Path(settings.MEDIA_ROOT))).replace("\\", "/")


def strip_accents_for_display(text: Any) -> str:
    """
    去掉法语重音，用于正式文件中的地址显示。

    例如：
        Pôle -> Pole
        Hôpital -> Hopital
        Rééducation -> Reeducation
    """
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) != "Mn"
    )
    return text


def normalize_address_display_line(line: Any) -> str:
    """
    地址显示标准化：
        1. 去重音
        2. 转大写
        3. 合并多余空格
    """
    text = strip_accents_for_display(line)
    text = text.upper()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_address_display_lines(lines: List[str]) -> List[str]:
    return [
        normalize_address_display_line(line)
        for line in lines
        if str(line).strip()
    ]


def split_text_lines(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    text = str(value).strip()

    if not text:
        return []

    lines = []

    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    return lines


def address_data_to_lines(address_data: Any) -> List[str]:
    """
    把 extraction 阶段保存的地址 JSON 转成模板需要的 list[str]。
    """
    if not address_data:
        return []

    if isinstance(address_data, list):
        return [str(x).strip() for x in address_data if str(x).strip()]

    if isinstance(address_data, str):
        return split_text_lines(address_data)

    if isinstance(address_data, dict):
        for key in ["display_lines", "lines", "address_lines"]:
            value = address_data.get(key)
            if isinstance(value, list) and value:
                return [str(x).strip() for x in value if str(x).strip()]

        result = []

        name_lines = address_data.get("name_lines")
        if isinstance(name_lines, list):
            result.extend([str(x).strip() for x in name_lines if str(x).strip()])

        for key in [
            "name",
            "street",
            "address",
            "postal_city",
            "city",
            "country",
        ]:
            value = address_data.get(key)
            if value:
                result.extend(split_text_lines(value))

        return result

    return []


def get_shipping_address_lines(order: Order) -> List[str]:
    """
    Invoice 中使用的 Shipping Address。

    正式文件中统一：
        去重音 + 大写
    """
    lines = address_data_to_lines(order.shipping_address_data)

    if not lines:
        extracted = order.extracted_order_data or {}
        lines = address_data_to_lines(
            extracted.get("addresses", {}).get("shipping_address_from_order")
        )

    return normalize_address_display_lines(lines)


def get_invoice_address_lines(order: Order) -> List[str]:
    """
    Invoice 中使用的 Invoice Address。

    正式文件中统一：
        去重音 + 大写
    """
    if order.hospital and order.hospital.billing_address:
        lines = split_text_lines(order.hospital.billing_address)
    else:
        lines = address_data_to_lines(order.billing_address_data)

        if not lines:
            extracted = order.extracted_order_data or {}
            lines = address_data_to_lines(
                extracted.get("addresses", {}).get("billing_address_from_order")
            )

    return normalize_address_display_lines(lines)


def get_po_shipping_address_lines(order: Order) -> List[str]:
    """
    Factory PO 中使用的 Shipping Address。

    与医院 Invoice 不同：
        PO 发给工厂，只需要最终送货地址；
        不需要医院名称、电话、传真、联系人。

    最终只保留：
        street
        postal_city
        country
    """
    shipping = order.shipping_address_data or {}

    if not isinstance(shipping, dict):
        extracted = order.extracted_order_data or {}
        shipping = (
            extracted.get("addresses", {})
            .get("shipping_address_from_order", {})
            or {}
        )

    lines = []

    street = shipping.get("street")
    postal_city = shipping.get("postal_city")
    country = shipping.get("country") or "France"

    if street:
        lines.append(street)

    if postal_city:
        lines.append(postal_city)

    if country:
        lines.append(country)

    if len(lines) < 2:
        fallback_lines = address_data_to_lines(shipping)
        cleaned = []

        for line in fallback_lines:
            simple = strip_accents_for_display(line).upper()

            if simple.startswith("TEL"):
                continue
            if simple.startswith("FAX"):
                continue
            if simple.startswith("CORRESPONDANT"):
                continue
            if "TELEPHONE" in simple:
                continue

            if (
                re.search(r"\b\d{5}\b", simple)
                or "RUE" in simple
                or "AVENUE" in simple
                or "BOULEVARD" in simple
                or "ROUTE" in simple
                or "PLACE" in simple
                or "CHEMIN" in simple
                or "IMPASSE" in simple
            ):
                cleaned.append(line)

        lines = cleaned

        joined = " ".join(lines).lower()
        if "france" not in joined:
            lines.append("France")

    return normalize_address_display_lines(lines[:3])


def get_successful_factory_confirmation(order: Order) -> FactoryConfirmation:
    confirmations = list(
        FactoryConfirmation.objects.filter(
            order=order,
            extraction_status=FactoryConfirmation.ExtractionStatus.SUCCESS,
        ).order_by("-created_at")
    )

    if not confirmations:
        raise ValueError(
            f"Order {order.bon_de_commande}: no successful FactoryConfirmation found."
        )

    return confirmations[0]


def ensure_order_can_generate(order: Order) -> Dict[str, Any]:
    result = validate_order_for_document_generation(order=order, save=True)

    if not result.get("can_generate_documents"):
        errors = result.get("errors") or []
        error_text = "; ".join(errors[:5])
        raise ValueError(
            f"Order {order.bon_de_commande} is not ready for document generation: "
            f"{error_text}"
        )

    return result


def build_invoice_items_from_order(order: Order) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    items: List[Dict[str, Any]] = []

    for item in order.items.all().order_by("id"):
        quantity = item.confirmed_quantity or 0

        if quantity <= 0:
            continue

        if item.product and item.product.hospital_unit_price is not None:
            unit_price = Decimal(item.product.hospital_unit_price)
        else:
            unit_price = Decimal(item.hospital_unit_price or 0)

        if unit_price <= 0:
            warnings.append(
                f"OrderItem {item.product_code}: hospital_unit_price is missing or zero."
            )

        amount = unit_price * Decimal(quantity)

        description = (
            item.product.description
            if item.product and item.product.description
            else item.description
        )

        items.append(
            {
                "product_code": item.product_code,
                "description": description or "",
                "quantity_raw": float(quantity),
                "quantity": format_quantity(float(quantity)),
                "unit_price_raw": float(unit_price),
                "unit_price": format_eur(float(unit_price)),
                "amount_raw": float(amount),
                "amount": format_eur(float(amount)),
            }
        )

    if not items:
        warnings.append("No confirmed OrderItem found for invoice.")

    return items, warnings


def build_invoice_serial_items(
    order: Order,
    confirmation: FactoryConfirmation,
) -> List[Dict[str, Any]]:
    serial_items = []

    serials = SerialItem.objects.filter(
        order=order,
        factory_confirmation=confirmation,
    ).order_by("id")

    for serial in serials:
        serial_items.append(
            {
                "product_code": serial.product_code,
                "quantity": format_serial_quantity(1.0),
                "serial_number": serial.serial_number or "",
                "expiration_date": (
                    serial.expiration_date.isoformat()
                    if serial.expiration_date
                    else ""
                ),
            }
        )

    return serial_items


def build_hospital_invoice_data(
    order: Order,
    confirmation: FactoryConfirmation,
    company_info: Dict[str, Any],
    numbers: Dict[str, Any],
    invoice_date,
) -> Dict[str, Any]:
    invoice_date = parse_document_date(invoice_date)
    invoice_date_dt = datetime.combine(invoice_date, datetime.min.time())

    payment_terms_days = int(company_info.get("payment_terms_days", 30))
    due_date = invoice_date + timedelta(days=payment_terms_days)
    due_date_dt = datetime.combine(due_date, datetime.min.time())

    items, warnings = build_invoice_items_from_order(order)

    serial_items = build_invoice_serial_items(
        order=order,
        confirmation=confirmation,
    )

    untaxed_amount = sum(
        float(item.get("amount_raw") or 0)
        for item in items
    )

    total_units = sum(
        float(item.get("quantity_raw") or 0)
        for item in items
    )

    vat = 0.0
    total = untaxed_amount + vat

    invoice_data = {
        "invoice": {
            "invoice_number": numbers["invoice_number"],
            "invoice_date": format_date_fr(invoice_date_dt),
            "due_date": format_date_fr(due_date_dt),
            "source": f"BON DE COMMANDE N° {order.bon_de_commande}",
            "payment_reference": str(order.bon_de_commande),
            "payment_terms_days": payment_terms_days,
        },
        "company": company_info,
        "addresses": {
            "shipping_address": get_shipping_address_lines(order),
            "invoice_address": get_invoice_address_lines(order),
        },
        "items": items,
        "serial_items": serial_items,
        "totals": {
            "untaxed_amount_raw": untaxed_amount,
            "vat_raw": vat,
            "total_raw": total,
            "total_units_raw": total_units,
            "untaxed_amount": format_eur(untaxed_amount),
            "vat": format_eur(vat),
            "total": format_eur(total),
            "total_units": format_quantity(total_units),
        },
        "debug": {
            "order_id": order.id,
            "bon_de_commande": order.bon_de_commande,
            "factory_confirmation_id": confirmation.id,
            "document_sequence": numbers,
        },
        "warnings": warnings,
    }

    return invoice_data


def should_apply_expiration_discount(expiration_date, document_date) -> bool:
    if not expiration_date:
        return False

    threshold_date = document_date + timedelta(days=EXPIRATION_THRESHOLD_DAYS)

    return expiration_date < threshold_date


def build_factory_po_items_from_serials(
    order: Order,
    confirmation: FactoryConfirmation,
    factory_info: Dict[str, Any],
    document_date,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []

    prepared_factory = prepare_factory_info(factory_info)

    default_description = prepared_factory.get(
        "default_product_description",
        "HT-Supreme™ Drug Eluting Stent",
    )

    order_items_by_code = {
        item.product_code: item
        for item in order.items.all()
    }

    groups: Dict[Tuple[str, Decimal], Dict[str, Any]] = {}

    serials = SerialItem.objects.filter(
        order=order,
        factory_confirmation=confirmation,
    ).order_by("id")

    for serial in serials:
        code = serial.product_code

        if not code:
            warnings.append(
                f"SerialItem {serial.id}: missing product_code."
            )
            continue

        discount_rate = (
            EXPIRATION_DISCOUNT_RATE
            if should_apply_expiration_discount(
                serial.expiration_date,
                document_date,
            )
            else Decimal("0.00")
        )

        order_item = order_items_by_code.get(code)

        if order_item and order_item.product:
            unit_price = Decimal(order_item.product.factory_unit_price or 0)
        else:
            unit_price = DEFAULT_FACTORY_UNIT_PRICE

        if unit_price <= 0:
            warnings.append(
                f"{code}: factory_unit_price is missing or zero, fallback to 120."
            )
            unit_price = DEFAULT_FACTORY_UNIT_PRICE

        key = (code, discount_rate)

        if key not in groups:
            groups[key] = {
                "product_code": code,
                "description": default_description,
                "quantity_raw": Decimal("0.00"),
                "unit_price_raw": unit_price,
                "discount_rate_raw": discount_rate,
                "serial_numbers": [],
                "expiration_dates": [],
                "min_expiration_date": serial.expiration_date,
            }

        delivered_quantity = Decimal("1.00")

        if serial.raw_data:
            raw_qty = serial.raw_data.get("delivered_quantity")
            if raw_qty is not None:
                try:
                    delivered_quantity = Decimal(str(raw_qty))
                except Exception:
                    warnings.append(
                        f"{code} / {serial.serial_number}: delivered_quantity invalid, use 1."
                    )
                    delivered_quantity = Decimal("1.00")

        groups[key]["quantity_raw"] += delivered_quantity

        if serial.serial_number:
            groups[key]["serial_numbers"].append(serial.serial_number)

        if serial.expiration_date:
            groups[key]["expiration_dates"].append(serial.expiration_date.isoformat())

            current_min = groups[key].get("min_expiration_date")
            if current_min is None or serial.expiration_date < current_min:
                groups[key]["min_expiration_date"] = serial.expiration_date

    order_sequence = [
        item.product_code
        for item in order.items.all().order_by("id")
    ]

    order_index = {
        code: index
        for index, code in enumerate(order_sequence)
    }

    sorted_groups = sorted(
        groups.values(),
        key=lambda item: (
            order_index.get(item["product_code"], 999999),
            item["product_code"],
            float(item["discount_rate_raw"]),
        ),
    )

    po_items: List[Dict[str, Any]] = []

    for group in sorted_groups:
        quantity = Decimal(group["quantity_raw"])
        unit_price = Decimal(group["unit_price_raw"])
        discount_rate = Decimal(group["discount_rate_raw"])

        amount = quantity * unit_price * (Decimal("1.00") - discount_rate)

        min_expiration_date = group.get("min_expiration_date")

        discount_note = ""
        if discount_rate > 0:
            if min_expiration_date:
                discount_note = (
                    f"30% discount applied. Earliest expiration: "
                    f"{min_expiration_date.isoformat()}"
                )
            else:
                discount_note = "30% discount applied due to expiration date."

        po_items.append(
            {
                "product_code": group["product_code"],
                "description": group["description"],
                "quantity_raw": float(quantity),
                "unit_price_raw": float(unit_price),
                "discount_rate_raw": float(discount_rate),
                "amount_raw": float(amount),
                "quantity": format_po_quantity(float(quantity)),
                "unit_price": format_po_unit_price(float(unit_price)),
                "discount": format_po_discount(float(discount_rate)),
                "amount": format_po_eur(float(amount)),
                "discount_note": discount_note,
                "serial_numbers": group["serial_numbers"],
                "expiration_dates": group["expiration_dates"],
                "min_expiration_date": (
                    min_expiration_date.isoformat()
                    if min_expiration_date
                    else None
                ),
            }
        )

    if not po_items:
        warnings.append("No PO item generated from SerialItem.")

    return po_items, warnings


def build_factory_po_data(
    order: Order,
    confirmation: FactoryConfirmation,
    company_info: Dict[str, Any],
    factory_info: Dict[str, Any],
    numbers: Dict[str, Any],
    po_order_date,
    shipping_date,
) -> Dict[str, Any]:
    po_order_date = parse_document_date(po_order_date)
    shipping_date = parse_document_date(shipping_date)
    expected_arrival = shipping_date + timedelta(days=EXPECTED_ARRIVAL_DAYS)

    company = prepare_company_info(company_info)
    factory = prepare_factory_info(factory_info)

    items, warnings = build_factory_po_items_from_serials(
        order=order,
        confirmation=confirmation,
        factory_info=factory_info,
        document_date=shipping_date,
    )

    total_raw = sum(
        float(item.get("amount_raw") or 0)
        for item in items
    )

    po_data = {
        "po": {
            "po_number": numbers["po_number"],
            "order_date": format_date_display(po_order_date),
            "expected_arrival": format_date_display(expected_arrival),
            "order_date_iso": po_order_date.isoformat(),
            "shipping_date_iso": shipping_date.isoformat(),
            "expected_arrival_iso": expected_arrival.isoformat(),
        },
        "company": company,
        "factory": factory,
        "shipping_address": get_po_shipping_address_lines(order),
        "items": items,
        "totals": {
            "total_raw": total_raw,
            "total": format_po_eur(total_raw),
        },
        "debug": {
            "order_id": order.id,
            "bon_de_commande": order.bon_de_commande,
            "factory_confirmation_id": confirmation.id,
            "document_sequence": numbers,
            "expiration_discount_threshold_days": EXPIRATION_THRESHOLD_DAYS,
            "expiration_discount_rate": float(EXPIRATION_DISCOUNT_RATE),
        },
        "warnings": warnings,
    }

    return po_data


def save_generated_document_record(
    order: Order,
    document_type: str,
    document_number: str,
    pdf_path: Path,
    html_path: Path,
    source_data: Dict[str, Any],
    generated_by,
) -> GeneratedDocument:
    obj, created = GeneratedDocument.objects.update_or_create(
        document_type=document_type,
        document_number=document_number,
        defaults={
            "order": order,
            "pdf_file": media_relative_path(pdf_path),
            "html_file": media_relative_path(html_path),
            "source_data": json_safe(source_data),
            "generated_by": generated_by,
        },
    )

    return obj


@transaction.atomic
def generate_hospital_invoice_for_order(
    order: Order,
    generated_by,
    document_date: Optional[Any] = None,
) -> Dict[str, Any]:
    validation = ensure_order_can_generate(order)
    confirmation = get_successful_factory_confirmation(order)

    sequence_date, sequence_date_source, sequence_date_warnings = resolve_factory_po_document_date(
        order=order,
        confirmation=confirmation,
        manual_date=document_date,
    )

    invoice_date, invoice_date_source, invoice_date_warnings = resolve_invoice_po_document_date(
        order=order,
        confirmation=confirmation,
        manual_date=None,
    )

    numbers = get_or_create_document_numbers(
        bon_de_commande=order.bon_de_commande,
        document_date=sequence_date,
    )

    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )

    invoice_data = build_hospital_invoice_data(
        order=order,
        confirmation=confirmation,
        company_info=company_info,
        numbers=numbers,
        invoice_date=invoice_date,
    )

    invoice_data.setdefault("warnings", []).extend(sequence_date_warnings)
    invoice_data.setdefault("warnings", []).extend(invoice_date_warnings)
    invoice_data.setdefault("debug", {})
    invoice_data["debug"]["sequence_date_source"] = sequence_date_source
    invoice_data["debug"]["invoice_date_source"] = invoice_date_source
    invoice_data["debug"]["sequence_date"] = sequence_date.isoformat()
    invoice_data["debug"]["invoice_date"] = invoice_date.isoformat()

    workspace = get_order_document_workspace(order) / "invoices"
    filename_base = sanitize_filename(numbers["invoice_number"])

    html_path = workspace / f"{filename_base}.html"
    pdf_path = workspace / f"{filename_base}.pdf"
    data_path = workspace / f"{filename_base}_data.json"

    html_content = render_invoice_html(
        invoice_data=invoice_data,
        template_dir=Path(settings.BASE_DIR) / "templates",
        template_name="hospital_invoice.html",
    )

    write_invoice_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=Path(settings.BASE_DIR),
    )

    source_data = {
        "validation": validation,
        "numbers": numbers,
        "invoice_data": invoice_data,
    }

    save_json_file(source_data, data_path)

    generated_document = save_generated_document_record(
        order=order,
        document_type="hospital_invoice",
        document_number=numbers["invoice_number"],
        pdf_path=pdf_path,
        html_path=html_path,
        source_data=source_data,
        generated_by=generated_by,
    )

    return {
        "generated_document_id": generated_document.id,
        "document_type": "hospital_invoice",
        "document_number": numbers["invoice_number"],
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "data_path": str(data_path),
        "warnings": invoice_data.get("warnings", []),
    }


@transaction.atomic
def generate_factory_po_for_order(
    order: Order,
    generated_by,
    document_date: Optional[Any] = None,
) -> Dict[str, Any]:
    validation = ensure_order_can_generate(order)
    confirmation = get_successful_factory_confirmation(order)

    po_order_date, po_order_date_source, po_order_date_warnings = resolve_factory_po_document_date(
        order=order,
        confirmation=confirmation,
        manual_date=document_date,
    )

    shipping_date, shipping_date_source, shipping_date_warnings = resolve_invoice_po_document_date(
        order=order,
        confirmation=confirmation,
        manual_date=None,
    )

    numbers = get_or_create_document_numbers(
        bon_de_commande=order.bon_de_commande,
        document_date=po_order_date,
    )

    company_info = load_json_config(
        Path(settings.BASE_DIR) / "config" / "company_info.json"
    )

    factory_info = build_factory_info_from_model(
        confirmation.factory or order.factory
    )

    po_data = build_factory_po_data(
        order=order,
        confirmation=confirmation,
        company_info=company_info,
        factory_info=factory_info,
        numbers=numbers,
        po_order_date=po_order_date,
        shipping_date=shipping_date,
    )

    po_data.setdefault("warnings", []).extend(po_order_date_warnings)
    po_data.setdefault("warnings", []).extend(shipping_date_warnings)
    po_data.setdefault("debug", {})
    po_data["debug"]["po_order_date_source"] = po_order_date_source
    po_data["debug"]["shipping_date_source"] = shipping_date_source
    po_data["debug"]["po_order_date"] = po_order_date.isoformat()
    po_data["debug"]["shipping_date"] = shipping_date.isoformat()
    po_data["debug"]["discount_reference_date"] = shipping_date.isoformat()

    workspace = get_order_document_workspace(order) / "purchase_orders"
    filename_base = sanitize_filename(f"Purchase_Order_{numbers['po_number']}")

    html_path = workspace / f"{filename_base}.html"
    pdf_path = workspace / f"{filename_base}.pdf"
    data_path = workspace / f"{filename_base}_data.json"

    html_content = render_po_html(
        po_data=po_data,
        template_path=Path(settings.BASE_DIR)
        / "templates"
        / "factory_purchase_order.html",
    )

    write_po_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=Path(settings.BASE_DIR),
    )

    source_data = {
        "validation": validation,
        "numbers": numbers,
        "po_data": po_data,
    }

    save_json_file(source_data, data_path)

    generated_document = save_generated_document_record(
        order=order,
        document_type="factory_po",
        document_number=numbers["po_number"],
        pdf_path=pdf_path,
        html_path=html_path,
        source_data=source_data,
        generated_by=generated_by,
    )

    return {
        "generated_document_id": generated_document.id,
        "document_type": "factory_po",
        "document_number": numbers["po_number"],
        "pdf_path": str(pdf_path),
        "html_path": str(html_path),
        "data_path": str(data_path),
        "warnings": po_data.get("warnings", []),
    }


@transaction.atomic
def generate_all_documents_for_order(
    order: Order,
    generated_by,
    document_date: Optional[Any] = None,
) -> Dict[str, Any]:
    invoice_result = generate_hospital_invoice_for_order(
        order=order,
        generated_by=generated_by,
        document_date=document_date,
    )

    po_result = generate_factory_po_for_order(
        order=order,
        generated_by=generated_by,
        document_date=document_date,
    )

    order.status = Order.Status.DOCUMENTS_GENERATED
    order.save(update_fields=["status", "updated_at"])

    return {
        "order_id": order.id,
        "bon_de_commande": order.bon_de_commande,
        "invoice": invoice_result,
        "factory_po": po_result,
    }
