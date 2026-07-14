# -*- coding: utf-8 -*-

"""
06_generate_factory_po.py

作用：
    根据医院订单提取结果和工厂确认文件，自动生成发给工厂的 Purchase Order PDF。

输入文件：
    1. outputs/extracted_order.json
       - 医院订单提取结果
       - 用于获取 Shipping Address 和医院订单中的产品顺序

    2. outputs/factory_confirmation.json
       - 工厂确认文件提取结果
       - 用于获取实际确认有货的产品、serial number、expiration date

    3. config/company_info.json
       - 公司固定信息
       - 用于 logo、公司名称、registration no.、PO 页眉中的香港公司地址

    4. config/factory_info.json
       - 工厂固定信息
       - 用于工厂名称、工厂地址、buyer

    5. config/document_registry.json
       - Invoice 和 PO 共用的统一编号流水记录
       - 初始可以是 {}
       - 同一个 bon_de_commande 会复用同一个月度流水号

    6. templates/factory_purchase_order.html
       - PO 的 HTML 模板

输出文件：
    purchase_orders/Purchase_Order_DELAHK0106S.html
    purchase_orders/Purchase_Order_DELAHK0106S.pdf
    purchase_orders/Purchase_Order_DELAHK0106S_data.json

当前业务规则：
    1. PO 编号：
        DELAHK + 本月第几单两位数 + 月份两位数 + S

        例如：
            2026 年 6 月第 1 单 -> DELAHK0106S
            2026 年 6 月第 2 单 -> DELAHK0206S

    2. Document Date / Order Date：
        如果命令行传入 --document-date，则使用传入日期。
        如果没有传入，则使用电脑本地日期。
        这个日期同时用于 PO Order Date、Expected Arrival 和过期日期折扣判断。

    3. Expected Arrival：
        Order Date + 3 days

    4. 日期显示格式：
        日/月/年
        例如：
            2026-06-02 -> 02/06/2026

    5. Shipping Address：
        PO 发给工厂，不需要显示电话、Fax 和收件人。
        所以只保留：
            - street
            - postal_city
            - France

    6. Unit Price：
        固定为 120.00

    7. Discount 严谨计算：
        对每一个 serial number 单独判断 expiration_date。
        如果 expiration_date < order_date + 365 days，则 discount = 30%。
        否则 discount = 0%。

    8. 分组逻辑：
        按 product_code + discount_rate 分组。
        这样同一个产品如果一部分小于一年有效期、一部分大于一年有效期，
        会拆成两行，避免金额计算错误。

    9. Amount：
        amount = unit_price * (1 - discount_rate) * quantity

    10. Total：
        所有 amount 加总。
"""

import argparse
import json
import re
import unicodedata
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

# 统一编号模块：保证同一个医院订单的 Invoice 和 PO 使用同一个流水号。
# 需要确保 src/document_numbering.py 已经存在。
from document_numbering import (
    parse_document_date,
    format_date_ddmmyyyy,
    get_document_numbers,
    get_bon_de_commande_from_order_data,
)


# ============================================================
# 1. 基础业务配置
# ============================================================

# 工厂采购单中所有产品统一使用的单价。
# 当前规则：Unit Price 全部为 120。
DEFAULT_FACTORY_UNIT_PRICE = 120.0

# 有效期小于一年时的折扣。
EXPIRATION_DISCOUNT_RATE = 0.30

# Expected Arrival = Order Date + 3 days。
EXPECTED_ARRIVAL_DAYS = 3

# 小于一年有效期的判断阈值。
# 这里使用 365 天。
EXPIRATION_THRESHOLD_DAYS = 365

# 产品编号格式，例如：
# BMA-2.5010
# BMA-4.5020
PRODUCT_RE = re.compile(
    r"BMA[-\s]*(\d)[\.,](\d{4})",
    re.IGNORECASE,
)


# ============================================================
# 2. JSON 读写工具
# ============================================================

def load_json(path: Path) -> Dict[str, Any]:
    """
    读取 JSON 文件。
    """
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Dict[str, Any], path: Path) -> None:
    """
    保存 JSON 文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 3. 文本与产品编号工具
# ============================================================

def normalize_product_code(text: str) -> Optional[str]:
    """
    标准化产品编号。

    输入可能是：
        BMA-2.5010
        BMA 2.5010
        BMA-2,5010

    输出统一为：
        BMA-2.5010
    """
    if not text:
        return None

    text = str(text).upper()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(",", ".")

    match = PRODUCT_RE.search(text)
    if not match:
        return None

    return f"BMA-{match.group(1)}.{match.group(2)}"


def strip_accents(text: str) -> str:
    """
    去掉法语重音。

    用途：
        - 判断地址行是否是电话 / Fax / Correspondant
        - 避免重音符号影响规则判断
    """
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text


def fix_address_display_text(text: str) -> str:
    """
    修正地址中常见 OCR 显示问题。

    注意：
        这里只做非常保守的显示修正。
        不影响产品编号、金额、serial number。
    """
    if text is None:
        return ""

    text = str(text)

    # 修正 PÔle87 / POLE87 / Pole87 -> Pôle87
    text = re.sub(
        r"\bP[ÔOÓÒÖ]LE\s*([0-9]+)\b",
        r"Pôle\1",
        text,
        flags=re.IGNORECASE,
    )

    return text


def sanitize_filename(text: str) -> str:
    """
    将 PO 编号转成适合作为文件名的形式。

    例如：
        DELAHK0106S -> DELAHK0106S
    """
    text = str(text).strip()
    text = re.sub(r"[^\w\-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


# ============================================================
# 4. 日期处理函数
# ============================================================

def parse_date_flexible(value: Any) -> Optional[date]:
    """
    尝试解析多种日期格式。

    支持：
        2026-06-02
        2026-06-02 13:49:55
        06/02/2026
        06/02/2026 13:49:55

    注意：
        工厂确认文件中的原始日期通常是美国格式 MM/DD/YYYY。
        但是本函数会优先尝试 ISO 格式，再尝试 MM/DD/YYYY。
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


# 注意：
#     Document Date / Order Date 的解析和显示格式，
#     现在统一由 src/document_numbering.py 负责：
#
#         parse_document_date()
#         format_date_ddmmyyyy()
#
#     因此本文件只保留 parse_date_flexible()，
#     用于解析工厂确认文件里的 expiration date。


# ============================================================
# 5. 数量、折扣、金额格式化
# ============================================================

def format_po_quantity(value: float) -> str:
    """
    PO 中数量显示为两位小数。

    例如：
        3 -> 3.00
        1 -> 1.00
    """
    return f"{float(value):.2f}"


def format_po_unit_price(value: float) -> str:
    """
    PO 中单价不带欧元符号。

    例如：
        120 -> 120.00
    """
    return f"{float(value):.2f}"


def format_po_discount(rate: float) -> str:
    """
    折扣显示为百分比。

    例如：
        0.0 -> 0.00%
        0.3 -> 30.00%
    """
    return f"{float(rate) * 100:.2f}%"


def format_po_eur(value: float) -> str:
    """
    PO 金额显示为英文金额格式。

    例如：
        2736 -> 2,736.00 €
    """
    return f"{float(value):,.2f} €"


# ============================================================
# 6. 统一编号说明
# ============================================================

# 注意：
#     旧版本中，这个脚本自己读取 config/po_registry.json，
#     并用 generate_po_number() / commit_po_number() 生成 PO 编号。
#
#     现在已经改成统一编号系统：
#         config/document_registry.json
#         src/document_numbering.py
#
#     好处：
#         同一个 bon_de_commande 只会分配一次月度流水号；
#         Invoice 和 PO 使用相同流水号，只是前缀和格式不同。
#
#     例如：
#         bon_de_commande = 150222
#         document_date = 2026-06-02
#         sequence = 01
#
#         Invoice Number = Invoice 20260106
#         PO Number      = DELAHK0106S
#
#     所以本文件不再保留独立 document_registry 逻辑。


# ============================================================
# 7. 公司信息与工厂信息整理
# ============================================================

def prepare_company_info(company_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    整理 company_info。

    PO 页眉中需要：
        DELA GLOBAL HK
        香港公司地址

    推荐在 company_info.json 中加入：

        "po_company": {
          "display_name": "DELA GLOBAL HK",
          "address": [
            "R1009,10/F, Front Block, Ming Sang Industrial Building",
            "19 Hing Yip Street Kwun Tong, KL",
            "Hong Kong"
          ]
        }

    如果没有写 po_company，代码会自动使用 fallback。
    """
    company = deepcopy(company_info)

    if "po_company" not in company:
        company["po_company"] = {
            "display_name": "DELA GLOBAL HK",
            "address": [
                "R1009,10/F, Front Block, Ming Sang Industrial Building",
                "19 Hing Yip Street Kwun Tong, KL",
                "Hong Kong",
            ],
        }

    if "logo_path" not in company:
        company["logo_path"] = "data/logo.png"

    if "company_name" not in company:
        company["company_name"] = "Dela Global Trade Consulting Limited"

    if "registration_no" not in company:
        company["registration_no"] = "71 99 26 22"

    return company


def prepare_factory_info(factory_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    整理 factory_info。

    推荐 config/factory_info.json 结构：

        {
          "factory_name": "Sino Medical Sciences Technology Inc",
          "factory_address": [
            "2nd Floor, TEDA Biopharma Res, Building B, #5",
            "4th St, TEDA, TIANJIN,301700,CHINA"
          ],
          "buyer": "Dela Medical Purchasing Department",
          "default_product_description": "HT-Supreme™ Drug Eluting Stent"
        }
    """
    factory = deepcopy(factory_info)

    factory.setdefault("factory_name", "Sino Medical Sciences Technology Inc")

    factory.setdefault(
        "factory_address",
        [
            "2nd Floor, TEDA Biopharma Res, Building B, #5",
            "4th St, TEDA, TIANJIN,301700,CHINA",
        ],
    )

    factory.setdefault("buyer", "Dela Medical Purchasing Department")

    factory.setdefault(
        "default_product_description",
        "HT-Supreme™ Drug Eluting Stent",
    )

    return factory


# ============================================================
# 8. Shipping Address 整理
# ============================================================

def build_shipping_address(order_data: Dict[str, Any]) -> List[str]:
    """
    从 extracted_order.json 中整理 PO 使用的 Shipping Address。

    重要：
        PO 发给工厂，不需要显示医院电话、传真、收件人。
        所以这里不像医院发票那样使用完整 display_lines。

    最终只保留三行：
        1. street
        2. postal_city
        3. France

    例如：
        87 AVENUE DU 69EME REGIMENT D'INFANTERIE
        54270 ESSEY-LES-NANCY
        France
    """
    addresses = order_data.get("addresses", {})
    shipping = addresses.get("shipping_address_from_order", {}) or {}

    lines: List[str] = []

    # ------------------------------------------------------------
    # 1. 优先使用新版 02_extract_order.py 提取出的结构化字段
    # ------------------------------------------------------------

    street = shipping.get("street")
    postal_city = shipping.get("postal_city")
    country = shipping.get("country") or "France"

    if street:
        lines.append(fix_address_display_text(str(street).strip()))

    if postal_city:
        lines.append(fix_address_display_text(str(postal_city).strip()))

    if country:
        lines.append(str(country).strip())

    # ------------------------------------------------------------
    # 2. 如果结构化字段没提取到，则从 display_lines 兜底
    # ------------------------------------------------------------

    if len(lines) < 2:
        display_lines = (
            shipping.get("display_lines")
            or addresses.get("raw_delivery_lines", [])
            or []
        )

        fallback_lines: List[str] = []

        for line in display_lines:
            text = str(line).strip()
            if not text:
                continue

            text_simple = strip_accents(text).upper()

            # 删除电话、传真、联系人
            if text_simple.startswith("TEL"):
                continue

            if text_simple.startswith("FAX"):
                continue

            if text_simple.startswith("CORRESPONDANT"):
                continue

            # 保留街道行
            if (
                "RUE" in text_simple
                or "AVENUE" in text_simple
                or "REGIMENT" in text_simple
                or "BOULEVARD" in text_simple
                or "ROUTE" in text_simple
                or "PLACE" in text_simple
            ):
                fallback_lines.append(fix_address_display_text(text))
                continue

            # 保留邮编城市行
            if re.search(r"\b\d{5}\b", text_simple):
                fallback_lines.append(fix_address_display_text(text))
                continue

            # 保留 France
            if "FRANCE" in text_simple:
                fallback_lines.append("France")
                continue

        lines = fallback_lines

    # ------------------------------------------------------------
    # 3. 自动补 France
    # ------------------------------------------------------------

    joined = " ".join(lines).lower()
    if "france" not in joined:
        lines.append("France")

    # ------------------------------------------------------------
    # 4. 最多保留三行
    # ------------------------------------------------------------

    return lines[:3]


# ============================================================
# 9. 医院订单中的产品顺序
# ============================================================

def get_order_product_sequence(order_data: Dict[str, Any]) -> List[str]:
    """
    从医院订单提取结果中读取产品顺序。

    这样 PO 产品表的顺序可以尽量和医院原始订单一致。
    """
    sequence: List[str] = []

    for item in order_data.get("items", []):
        raw_code = (
            item.get("product_code")
            or item.get("raw_product_text")
            or ""
        )

        code = normalize_product_code(raw_code)

        if code and code not in sequence:
            sequence.append(code)

    return sequence


# ============================================================
# 10. 折扣判断和 PO 产品行生成
# ============================================================

def should_apply_expiration_discount(
    expiration_date: Optional[date],
    order_date: date,
) -> bool:
    """
    判断是否应用 30% 折扣。

    规则：
        如果 expiration_date < order_date + 365 days，
        说明有效期小于一年，折扣 30%。

    如果 expiration_date 缺失：
        返回 False。
        同时会在上层生成 warning。
    """
    if expiration_date is None:
        return False

    threshold_date = order_date + timedelta(days=EXPIRATION_THRESHOLD_DAYS)

    return expiration_date < threshold_date


def build_po_items(
    order_data: Dict[str, Any],
    factory_data: Dict[str, Any],
    factory_info: Dict[str, Any],
    order_date: date,
    show_discount_note: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    根据 factory_confirmation.json 生成 PO 产品行。

    严谨版折扣逻辑：
        1. 遍历每个 serial item。
        2. 单独根据 expiration_date 判断折扣。
        3. 按 product_code + discount_rate 分组。
        4. 每组生成一行 PO item。

    为什么这样做：
        同一个产品型号可能有多个 serial number。
        其中一部分有效期小于一年，另一部分不小于一年。
        如果直接按产品型号汇总，会导致折扣和金额错误。
    """
    warnings: List[str] = []

    default_description = factory_info.get(
        "default_product_description",
        "HT-Supreme™ Drug Eluting Stent",
    )

    groups: Dict[Tuple[str, float], Dict[str, Any]] = {}

    serial_items = factory_data.get("serial_items", [])

    if not serial_items:
        warnings.append("factory_confirmation.json 中没有 serial_items。")

    for serial in serial_items:
        code = normalize_product_code(serial.get("product_code", ""))

        if not code:
            warnings.append(
                f"发现无法解析 product_code 的 serial item：{serial}"
            )
            continue

        delivered_qty = serial.get("delivered_quantity", 1.0)

        try:
            qty = float(delivered_qty)
        except (TypeError, ValueError):
            qty = 1.0
            warnings.append(
                f"{code} 的 delivered_quantity 无法解析，已按 1.0 处理。"
            )

        expiration_date_raw = (
            serial.get("expiration_date_iso")
            or serial.get("expiration_date_raw")
            or ""
        )

        expiration_date = parse_date_flexible(expiration_date_raw)

        if not expiration_date:
            warnings.append(
                f"{code} 缺少或无法解析 expiration date，"
                f"该 serial 默认按 0% 折扣处理。serial={serial.get('serial_number')}"
            )

        discount_rate = (
            EXPIRATION_DISCOUNT_RATE
            if should_apply_expiration_discount(expiration_date, order_date)
            else 0.0
        )

        key = (code, discount_rate)

        if key not in groups:
            groups[key] = {
                "product_code": code,
                "discount_rate": discount_rate,
                "quantity_raw": 0.0,
                "serial_numbers": [],
                "expiration_dates": [],
                "min_expiration_date": expiration_date,
                "description": default_description,
            }

        groups[key]["quantity_raw"] += qty

        if serial.get("serial_number"):
            groups[key]["serial_numbers"].append(serial.get("serial_number"))

        if expiration_date:
            groups[key]["expiration_dates"].append(expiration_date.isoformat())

            current_min = groups[key].get("min_expiration_date")
            if current_min is None or expiration_date < current_min:
                groups[key]["min_expiration_date"] = expiration_date

    order_sequence = get_order_product_sequence(order_data)
    order_index = {
        code: index
        for index, code in enumerate(order_sequence)
    }

    def group_sort_key(item: Dict[str, Any]) -> Tuple[int, str, float]:
        code = item["product_code"]
        discount_rate = item["discount_rate"]

        return (
            order_index.get(code, 999999),
            code,
            discount_rate,
        )

    sorted_groups = sorted(groups.values(), key=group_sort_key)

    po_items: List[Dict[str, Any]] = []

    for group in sorted_groups:
        code = group["product_code"]
        quantity = float(group["quantity_raw"])
        discount_rate = float(group["discount_rate"])

        unit_price = DEFAULT_FACTORY_UNIT_PRICE
        amount = unit_price * (1.0 - discount_rate) * quantity

        discount_note = ""

        if show_discount_note and discount_rate > 0:
            min_exp = group.get("min_expiration_date")
            if isinstance(min_exp, date):
                discount_note = (
                    f"30% discount applied. Earliest expiration: {min_exp.isoformat()}"
                )
            else:
                discount_note = "30% discount applied due to expiration date."

        po_items.append({
            "product_code": code,
            "description": group["description"],

            # 原始数值，方便保存到 debug JSON。
            "quantity_raw": quantity,
            "unit_price_raw": unit_price,
            "discount_rate_raw": discount_rate,
            "amount_raw": amount,

            # 显示值，直接给 HTML 模板使用。
            "quantity": format_po_quantity(quantity),
            "unit_price": format_po_unit_price(unit_price),
            "discount": format_po_discount(discount_rate),
            "amount": format_po_eur(amount),

            # 可选显示备注。默认空，不影响样本式排版。
            "discount_note": discount_note,

            # debug 信息，PDF 不一定显示，但 data JSON 中会保存。
            "serial_numbers": group["serial_numbers"],
            "expiration_dates": group["expiration_dates"],
            "min_expiration_date": (
                group["min_expiration_date"].isoformat()
                if isinstance(group.get("min_expiration_date"), date)
                else None
            ),
        })

    return po_items, warnings


# ============================================================
# 11. PO 数据组装
# ============================================================

def build_factory_po_data(
    order_data: Dict[str, Any],
    factory_data: Dict[str, Any],
    company_info: Dict[str, Any],
    factory_info: Dict[str, Any],
    registry_path: Path,
    document_date: date,
    preview: bool,
    show_discount_note: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    组装模板需要的全部 PO 数据。

    重要变化：
        旧版：
            PO 自己使用 po_registry.json 和 order_date 生成编号。

        新版：
            使用 document_numbering.py + document_registry.json。
            Invoice 和 PO 共用同一个 bon_de_commande 的流水号。

    document_date：
        这是“文件生成日期”。
        它同时用于：
            1. PO Order Date
            2. Expected Arrival = document_date + 3 days
            3. expiration date 折扣判断
            4. document_registry.json 的月份 key
    """
    warnings: List[str] = []

    company = prepare_company_info(company_info)
    factory = prepare_factory_info(factory_info)

    # 1. 从医院订单中读取 bon_de_commande。
    #    这是统一编号系统的核心 key。
    try:
        bon_de_commande = get_bon_de_commande_from_order_data(order_data)
    except Exception:
        # 兜底：如果 order_data 中缺失，则尝试从 factory confirmation 中读取。
        bon_de_commande = (
            factory_data.get("factory_document", {}).get("bon_de_commande")
            or factory_data.get("header", {}).get("bon_de_commande")
        )

        if not bon_de_commande:
            raise ValueError(
                "无法从医院订单或工厂确认文件中找到 bon_de_commande，"
                "不能生成统一编号。"
            )

    # 2. 统一生成文档编号。
    #    numbers 中同时包含 invoice_number 和 po_number。
    #    这里 PO 脚本只使用 po_number。
    numbers, number_meta = get_document_numbers(
        bon_de_commande=bon_de_commande,
        document_date=document_date,
        registry_path=registry_path,
        preview=preview,
    )

    po_number = numbers["po_number"]

    # 3. Expected Arrival = document_date + 3 days。
    expected_arrival = document_date + timedelta(days=EXPECTED_ARRIVAL_DAYS)

    # 4. Shipping Address：只保留三行，逻辑在 build_shipping_address() 中。
    shipping_address = build_shipping_address(order_data)

    # 5. 产品行：
    #    折扣判断使用 document_date，而不是工厂文件里的 shipping date。
    items, item_warnings = build_po_items(
        order_data=order_data,
        factory_data=factory_data,
        factory_info=factory,
        order_date=document_date,
        show_discount_note=show_discount_note,
    )

    warnings.extend(item_warnings)

    total_raw = sum(float(item.get("amount_raw", 0) or 0) for item in items)

    po_data: Dict[str, Any] = {
        "po": {
            "po_number": po_number,

            # 显示给 PDF 的日期：日/月/年
            "order_date": format_date_ddmmyyyy(document_date),
            "expected_arrival": format_date_ddmmyyyy(expected_arrival),

            # debug 用 ISO 格式
            "order_date_iso": document_date.isoformat(),
            "expected_arrival_iso": expected_arrival.isoformat(),

            # 保留 bon de commande，方便后续追踪。
            "bon_de_commande": numbers["bon_de_commande"],
        },

        "company": company,

        "factory": factory,

        "shipping_address": shipping_address,

        "items": items,

        "totals": {
            "total_raw": total_raw,
            "total": format_po_eur(total_raw),
        },

        "debug": {
            "document_numbering": {
                "sequence": numbers["sequence"],
                "invoice_number_for_same_order": numbers["invoice_number"],
                "po_number": numbers["po_number"],
                "month_key": numbers["month_key"],
                "bon_de_commande": numbers["bon_de_commande"],
                "is_existing_order": numbers["is_existing_order"],
                "preview": numbers["preview"],
                "meta": number_meta,
            },
            "expiration_discount_threshold_days": EXPIRATION_THRESHOLD_DAYS,
            "expiration_discount_rate": EXPIRATION_DISCOUNT_RATE,
            "factory_unit_price": DEFAULT_FACTORY_UNIT_PRICE,
            "document_date_iso": document_date.isoformat(),
        },

        "warnings": warnings,
    }

    return po_data, number_meta, warnings


# ============================================================
# 12. 数据检查
# ============================================================

def validate_po_data(po_data: Dict[str, Any]) -> List[str]:
    """
    生成 PDF 前进行基本检查。
    """
    errors: List[str] = []

    if not po_data.get("po", {}).get("po_number"):
        errors.append("缺少 PO 编号。")

    if not po_data.get("po", {}).get("order_date"):
        errors.append("缺少 Order Date。")

    if not po_data.get("po", {}).get("expected_arrival"):
        errors.append("缺少 Expected Arrival。")

    if not po_data.get("shipping_address"):
        errors.append("缺少 Shipping Address。")

    factory = po_data.get("factory", {})
    if not factory.get("factory_name"):
        errors.append("缺少工厂名称。")

    if not factory.get("factory_address"):
        errors.append("缺少工厂地址。")

    if not po_data.get("items"):
        errors.append("没有可生成 PO 的产品行。")

    return errors


# ============================================================
# 13. HTML / PDF 生成
# ============================================================

def render_po_html(
    po_data: Dict[str, Any],
    template_path: Path,
) -> str:
    """
    使用 Jinja2 渲染 HTML。
    """
    template_dir = template_path.parent
    template_name = template_path.name

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    template = env.get_template(template_name)

    return template.render(**po_data)


def write_html_and_pdf(
    html_content: str,
    html_path: Path,
    pdf_path: Path,
    project_root: Path,
) -> None:
    """
    保存 HTML，并使用 WeasyPrint 生成 PDF。

    base_url 必须设置为项目根目录，这样 HTML 中的 logo_path：
        data/logo.png
    才能被 WeasyPrint 正确找到。
    """
    html_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    base_url = project_root.resolve().as_uri() + "/"

    HTML(
        string=html_content,
        base_url=base_url,
    ).write_pdf(str(pdf_path))


# ============================================================
# 14. 主生成流程
# ============================================================

def generate_factory_po(
    order_json_path: Path,
    factory_json_path: Path,
    company_info_path: Path,
    factory_info_path: Path,
    registry_path: Path,
    template_path: Path,
    out_dir: Path,
    project_root: Path,
    document_date_arg: Optional[str],
    preview: bool,
    allow_warnings: bool,
    show_discount_note: bool,
) -> Dict[str, Any]:
    """
    生成工厂 PO 的主函数。
    """
    print("[INFO] 读取医院订单 extracted_order.json...")
    order_data = load_json(order_json_path)

    print("[INFO] 读取工厂确认 factory_confirmation.json...")
    factory_data = load_json(factory_json_path)

    print("[INFO] 读取公司信息 company_info.json...")
    company_info = load_json(company_info_path)

    print("[INFO] 读取工厂信息 factory_info.json...")
    factory_info = load_json(factory_info_path)

    print("[INFO] 确定 Document Date / Order Date...")
    document_date = parse_document_date(document_date_arg)
    print(f"[INFO] Document Date = {document_date.isoformat()}")

    print("[INFO] 组装 PO 数据...")
    po_data, number_meta, warnings = build_factory_po_data(
        order_data=order_data,
        factory_data=factory_data,
        company_info=company_info,
        factory_info=factory_info,
        registry_path=registry_path,
        document_date=document_date,
        preview=preview,
        show_discount_note=show_discount_note,
    )

    if warnings:
        print("[WARNING] 发现非致命警告：")
        for warning in warnings:
            print(f"  - {warning}")

    print("[INFO] 检查 PO 数据...")
    errors = validate_po_data(po_data)

    if errors:
        print("[ERROR] 发现严重错误：")
        for error in errors:
            print(f"  - {error}")

        if not allow_warnings:
            raise RuntimeError(
                "PO 数据存在严重错误，已停止生成。"
                "如果只是测试模板，可加 --allow-warnings。"
            )

    print("[INFO] 渲染 HTML 模板...")
    html_content = render_po_html(
        po_data=po_data,
        template_path=template_path,
    )

    po_number = po_data["po"]["po_number"]
    file_stem = f"Purchase_Order_{sanitize_filename(po_number)}"

    html_path = out_dir / f"{file_stem}.html"
    pdf_path = out_dir / f"{file_stem}.pdf"
    data_path = out_dir / f"{file_stem}_data.json"

    print("[INFO] 生成 HTML 和 PDF...")
    write_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=project_root,
    )

    print("[INFO] 保存 PO 数据 JSON...")
    save_json(po_data, data_path)

    if not preview:
        print("[INFO] 正式模式：已通过 document_numbering.py 更新 document_registry。")
    else:
        print("[INFO] Preview 模式：不更新 document_registry。")

    result = {
        "po_number": po_number,
        "order_date": po_data["po"]["order_date"],
        "expected_arrival": po_data["po"]["expected_arrival"],
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
        "data_path": str(data_path),
        "preview": preview,
        "warnings": warnings,
        "errors": errors,
    }

    return result


# ============================================================
# 15. 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate factory Purchase Order HTML/PDF."
    )

    parser.add_argument(
        "--order",
        type=str,
        default="outputs/extracted_order.json",
        help="医院订单提取结果 JSON。",
    )

    parser.add_argument(
        "--factory",
        type=str,
        default="outputs/factory_confirmation.json",
        help="工厂确认提取结果 JSON。",
    )

    parser.add_argument(
        "--company",
        type=str,
        default="config/company_info.json",
        help="公司固定信息 JSON。",
    )

    parser.add_argument(
        "--factory-info",
        type=str,
        default="config/factory_info.json",
        help="工厂固定信息 JSON。",
    )

    parser.add_argument(
        "--registry",
        type=str,
        default="config/document_registry.json",
        help="统一文档编号流水记录 JSON。Invoice 和 PO 共用。初始可以是 {}。",
    )

    parser.add_argument(
        "--template",
        type=str,
        default="templates/factory_purchase_order.html",
        help="PO HTML 模板。",
    )

    parser.add_argument(
        "--out-dir",
        type=str,
        default="purchase_orders",
        help="PO 输出文件夹。",
    )

    parser.add_argument(
        "--document-date",
        type=str,
        default=None,
        help=(
            "可选：手动指定文件生成日期，格式 YYYY-MM-DD。"
            "如果不传，则使用电脑本地日期。"
            "该日期同时用于 PO Order Date、Expected Arrival 和统一编号。"
        ),
    )

    parser.add_argument(
        "--order-date",
        type=str,
        default=None,
        help=(
            "兼容旧命令的参数，已不推荐使用。"
            "如果同时提供 --document-date 和 --order-date，优先使用 --document-date。"
        ),
    )

    parser.add_argument(
        "--preview",
        action="store_true",
        help="预览模式：生成 HTML/PDF，但不更新 document_registry。",
    )

    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="即使有严重错误也继续生成，用于调试模板。不建议正式使用。",
    )

    parser.add_argument(
        "--show-discount-note",
        action="store_true",
        help="如果产品有 30% 折扣，在 Description 下方显示折扣原因备注。",
    )

    args = parser.parse_args()

    project_root = Path.cwd()

    result = generate_factory_po(
        order_json_path=Path(args.order),
        factory_json_path=Path(args.factory),
        company_info_path=Path(args.company),
        factory_info_path=Path(args.factory_info),
        registry_path=Path(args.registry),
        template_path=Path(args.template),
        out_dir=Path(args.out_dir),
        project_root=project_root,
        document_date_arg=(args.document_date or args.order_date),
        preview=args.preview,
        allow_warnings=args.allow_warnings,
        show_discount_note=args.show_discount_note,
    )

    print("\n==============================")
    print("[DONE] 工厂 Purchase Order 生成完成")
    print("==============================")
    print(f"PO Number: {result['po_number']}")
    print(f"Order Date: {result['order_date']}")
    print(f"Expected Arrival: {result['expected_arrival']}")
    print(f"HTML: {result['html_path']}")
    print(f"PDF: {result['pdf_path']}")
    print(f"Data JSON: {result['data_path']}")
    print(f"Preview mode: {result['preview']}")

    if result["warnings"]:
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"- {warning}")

    if result["errors"]:
        print("\nErrors:")
        for error in result["errors"]:
            print(f"- {error}")


if __name__ == "__main__":
    main()