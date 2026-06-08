# -*- coding: utf-8 -*-

"""
03_extract_factory_confirmation.py

作用：
    从工厂发回来的确认 PDF 中提取库存确认信息。

输入：
    工厂确认 PDF，例如：
        data/factory_confirmation.pdf

输出：
    factory_confirmation.json，例如：
        outputs/factory_confirmation.json

提取内容：
    1. WH/OUT 编号
    2. BON DE COMMANDE 编号
    3. Shipping Date 发货日期
    4. Total Demand
    5. Total Completed
    6. 每一支产品的：
        - product_code
        - serial_number
        - expiration_date
        - delivered_quantity
    7. 按 product_code 汇总 confirmed_quantity

注意：
    这个脚本只负责“提取工厂文件信息”。
    它不负责判断哪些产品缺货。
    缺货判断要在下一步：
        医院订单 extracted_order.json
        +
        工厂确认 factory_confirmation.json
        进行比对。
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF


# ============================================================
# 1. 正则表达式定义
# ============================================================

# 产品编号，例如：
# BMA-4.5020
# BMA-2.5010
PRODUCT_RE = re.compile(
    r"BMA[-\s]*(\d)[\.,](\d{4})",
    re.IGNORECASE,
)

# WH/OUT 编号，例如：
# WH/OUT/00269
WAREHOUSE_OUT_RE = re.compile(
    r"\bWH/OUT/\d+\b",
    re.IGNORECASE,
)

# Bon de commande，例如：
# BON DE COMMANDE N° 150222
BON_RE = re.compile(
    r"BON\s+DE\s+COMMANDE\s+N[°o]?\s*([0-9]+)",
    re.IGNORECASE,
)

# Shipping Date，例如：
# Shipping Date:
# 06/01/2026 13:49:55
#
# 这里支持两种情况：
# 1. Shipping Date: 06/01/2026 13:49:55
# 2. Shipping Date:\n06/01/2026 13:49:55
SHIPPING_DATE_RE = re.compile(
    r"Shipping\s+Date\s*[:：]?\s*"
    r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)

# Total Demand / Total Completed
# 注意原 PDF 里可能是英文冒号 ":"，也可能被解析成中文全角冒号 "："
TOTAL_DEMAND_RE = re.compile(
    r"Total\s+Demand\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

TOTAL_COMPLETED_RE = re.compile(
    r"Total\s+Completed\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

# 表格行格式，例如：
# BMA-4.5020 084927051326C0758001 05/13/2027 00:00:00 1.00 Units 9021 90
#
# 提取：
# 1. Product Code       BMA-4.5020
# 2. Serial Number      084927051326C0758001
# 3. Expiration Date    05/13/2027
# 4. Expiration Time    00:00:00
# 5. Delivered Qty      1.00
#
# HS Code 后面不需要，所以不捕获。
SERIAL_ROW_RE = re.compile(
    r"\b(BMA[-\s]*\d[\.,]\d{4})\s+"
    r"([A-Z0-9]{8,})\s+"
    r"(\d{2}/\d{2}/\d{4})\s+"
    r"(\d{2}:\d{2}:\d{2})\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+Units\b",
    re.IGNORECASE,
)


# ============================================================
# 2. 基础工具函数
# ============================================================

def normalize_product_code(raw: str) -> Optional[str]:
    """
    将 OCR / PDF 文本中的产品编号标准化。

    输入可能是：
        BMA-4.5020
        BMA 4.5020
        BMA-4,5020

    输出统一为：
        BMA-4.5020

    如果没有识别到产品编号，返回 None。
    """
    if not raw:
        return None

    text = str(raw).upper()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(",", ".")

    match = PRODUCT_RE.search(text)
    if not match:
        return None

    diameter_part = match.group(1)
    length_part = match.group(2)

    return f"BMA-{diameter_part}.{length_part}"


def parse_float(value: Optional[str]) -> Optional[float]:
    """
    将字符串转成 float。
    如果失败，返回 None。
    """
    if value is None:
        return None

    try:
        return float(str(value).strip())
    except ValueError:
        return None


def parse_factory_date_to_iso(date_raw: Optional[str]) -> Optional[str]:
    """
    将工厂文件中的日期转换成 ISO 格式：YYYY-MM-DD。

    重要：
        工厂文件中的日期应按美国格式 MM/DD/YYYY 理解。

    原因：
        文件里有类似 05/13/2027 的日期。
        13 不可能是月份，所以这里一定是：
            05/13/2027 = 2027-05-13

    输入：
        05/13/2027
        06/01/2026 13:49:55

    输出：
        2027-05-13
        2026-06-01
    """
    if not date_raw:
        return None

    date_raw = str(date_raw).strip()

    # 可能是 "06/01/2026 13:49:55"
    # 也可能是 "05/13/2027"
    for fmt in ["%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]:
        try:
            dt = datetime.strptime(date_raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def parse_factory_datetime_to_iso(date_raw: Optional[str]) -> Optional[str]:
    """
    将 Shipping Date 转换成 ISO datetime。

    输入：
        06/01/2026 13:49:55

    输出：
        2026-06-01 13:49:55
    """
    if not date_raw:
        return None

    try:
        dt = datetime.strptime(date_raw.strip(), "%m/%d/%Y %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def clean_text(text: str) -> str:
    """
    清理 PDF 提取出来的文本。

    主要做：
        - 统一换行
        - 去掉奇怪的不可见字符
        - 保留基本结构
    """
    if not text:
        return ""

    # 替换一些 PDF 解析时可能出现的特殊字符
    text = text.replace("\uFFFE", "")
    text = text.replace("\ufeff", "")
    text = text.replace("\xa0", " ")

    # 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 去掉每一行首尾空格
    lines = [line.strip() for line in text.split("\n")]

    # 删除连续空行，但保留基本行结构
    cleaned_lines = []
    previous_empty = False

    for line in lines:
        if not line:
            if not previous_empty:
                cleaned_lines.append("")
            previous_empty = True
        else:
            cleaned_lines.append(line)
            previous_empty = False

    return "\n".join(cleaned_lines).strip()


# ============================================================
# 3. 读取 PDF 文本
# ============================================================

def read_pdf_text(pdf_path: Path) -> Tuple[List[Dict[str, Any]], str]:
    """
    读取 PDF 所有页面的文本。

    返回：
        pages_text:
            [
                {
                    "page": 1,
                    "text": "..."
                },
                {
                    "page": 2,
                    "text": "..."
                }
            ]

        full_text:
            所有页面合并后的文本

    说明：
        这份工厂文件大概率是系统导出的电子 PDF，
        所以优先用 PyMuPDF 直接提取文本，而不是 OCR。
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到 PDF 文件：{pdf_path}")

    doc = fitz.open(pdf_path)

    pages_text: List[Dict[str, Any]] = []

    for page_index in range(len(doc)):
        page = doc[page_index]

        # "text" 模式会按阅读顺序提取文本
        raw_text = page.get_text("text")
        text = clean_text(raw_text)

        pages_text.append(
            {
                "page": page_index + 1,
                "text": text,
            }
        )

    full_text = "\n".join(page["text"] for page in pages_text)
    full_text = clean_text(full_text)

    return pages_text, full_text


# ============================================================
# 4. 提取文件头信息
# ============================================================

def extract_factory_header(full_text: str) -> Dict[str, Any]:
    """
    从全文中提取工厂确认文件头部信息：

    - warehouse_out
    - bon_de_commande
    - shipping_date
    - total_demand
    - total_completed
    """
    warehouse_match = WAREHOUSE_OUT_RE.search(full_text)
    bon_match = BON_RE.search(full_text)
    shipping_match = SHIPPING_DATE_RE.search(full_text)
    demand_match = TOTAL_DEMAND_RE.search(full_text)
    completed_match = TOTAL_COMPLETED_RE.search(full_text)

    shipping_date_raw = shipping_match.group(1) if shipping_match else None

    total_demand = parse_float(demand_match.group(1)) if demand_match else None
    total_completed = parse_float(completed_match.group(1)) if completed_match else None

    return {
        "warehouse_out": warehouse_match.group(0) if warehouse_match else None,
        "bon_de_commande": bon_match.group(1) if bon_match else None,

        "shipping_date_raw": shipping_date_raw,
        "shipping_date_iso": parse_factory_datetime_to_iso(shipping_date_raw),
        "shipping_date_only_iso": parse_factory_date_to_iso(shipping_date_raw),

        "total_demand": total_demand,
        "total_completed": total_completed,
    }


# ============================================================
# 5. 提取表格中的 serial items
# ============================================================

def extract_serial_items_from_page(page_text: str, page_number: int) -> List[Dict[str, Any]]:
    """
    从单页文本中提取所有 serial item。

    每一行类似：
        BMA-4.5020 084927051326C0758001 05/13/2027 00:00:00 1.00 Units 9021 90

    输出：
        [
            {
                "page": 1,
                "product_code": "BMA-4.5020",
                "serial_number": "084927051326C0758001",
                "expiration_date_raw": "05/13/2027 00:00:00",
                "expiration_date_iso": "2027-05-13",
                "delivered_quantity": 1.0,
                "raw_line": "..."
            }
        ]
    """
    serial_items: List[Dict[str, Any]] = []

    # 方式 1：
    # 按整页文本用正则全局匹配。
    # 这样即使 PDF 每一行的空格数量不同，也能匹配。
    for match in SERIAL_ROW_RE.finditer(page_text):
        raw_product_code = match.group(1)
        serial_number = match.group(2)
        exp_date = match.group(3)
        exp_time = match.group(4)
        delivered_qty_raw = match.group(5)

        product_code = normalize_product_code(raw_product_code)

        expiration_date_raw = f"{exp_date} {exp_time}"
        delivered_quantity = parse_float(delivered_qty_raw)

        # 保存原始匹配文本，方便后续人工检查
        raw_line = match.group(0)

        serial_items.append(
            {
                "page": page_number,
                "product_code": product_code,
                "serial_number": serial_number,
                "expiration_date_raw": expiration_date_raw,
                "expiration_date_iso": parse_factory_date_to_iso(exp_date),
                "delivered_quantity": delivered_quantity,
                "raw_line": raw_line,
            }
        )

    return serial_items


def extract_all_serial_items(pages_text: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    从所有页面提取 serial items。
    """
    all_items: List[Dict[str, Any]] = []

    for page in pages_text:
        page_number = page["page"]
        page_text = page["text"]

        page_items = extract_serial_items_from_page(
            page_text=page_text,
            page_number=page_number,
        )

        all_items.extend(page_items)

    return all_items


# ============================================================
# 6. 按产品编号汇总
# ============================================================

def summarize_by_product(serial_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将一支一支的 serial items 按 product_code 汇总。

    例如：
        BMA-3.5015 有 3 个 serial number

    输出：
        {
            "product_code": "BMA-3.5015",
            "confirmed_quantity": 3,
            "serial_numbers": [...],
            "expiration_dates": [...]
        }
    """
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for item in serial_items:
        product_code = item.get("product_code")
        if product_code:
            grouped[product_code].append(item)

    summary: List[Dict[str, Any]] = []

    for product_code in sorted(grouped.keys()):
        items = grouped[product_code]

        serial_numbers = [
            item["serial_number"]
            for item in items
            if item.get("serial_number")
        ]

        expiration_dates = [
            item["expiration_date_iso"]
            for item in items
            if item.get("expiration_date_iso")
        ]

        delivered_quantity_sum = sum(
            item.get("delivered_quantity") or 0
            for item in items
        )

        summary.append(
            {
                "product_code": product_code,

                # confirmed_quantity 用 serial number 行数计算。
                # 因为每个支架应该对应一个 serial number。
                "confirmed_quantity": len(items),

                # 同时保留 delivered_quantity_sum 作为内部校验。
                "delivered_quantity_sum": delivered_quantity_sum,

                "serial_numbers": serial_numbers,
                "expiration_dates": expiration_dates,

                # 保留每个 serial 的明细，后面生成医院发票时会用到。
                "serial_allocations": [
                    {
                        "serial_number": item.get("serial_number"),
                        "expiration_date_iso": item.get("expiration_date_iso"),
                        "expiration_date_raw": item.get("expiration_date_raw"),
                        "delivered_quantity": item.get("delivered_quantity"),
                        "page": item.get("page"),
                    }
                    for item in items
                ],
            }
        )

    return summary


# ============================================================
# 7. 工厂文件内部校验
# ============================================================

def validate_factory_result(result: Dict[str, Any]) -> List[str]:
    """
    对工厂确认文件提取结果做内部校验。

    注意：
        这里只检查“工厂文件自身是否合理”。
        不检查是否满足医院订单。
        医院订单和工厂确认的比对放到下一步脚本。
    """
    warnings: List[str] = []

    doc_info = result.get("factory_document", {})
    serial_items = result.get("serial_items", [])

    # 1. 基本字段检查
    if not doc_info.get("warehouse_out"):
        warnings.append("没有提取到 WH/OUT 编号。")

    if not doc_info.get("bon_de_commande"):
        warnings.append("没有提取到 BON DE COMMANDE 编号。")

    if not doc_info.get("shipping_date_raw"):
        warnings.append("没有提取到 Shipping Date。")

    if doc_info.get("shipping_date_raw") and not doc_info.get("shipping_date_iso"):
        warnings.append(
            f"Shipping Date 日期格式无法解析：{doc_info.get('shipping_date_raw')}"
        )

    if not serial_items:
        warnings.append("没有提取到任何 Product / Serial Number 表格行。")

    # 2. serial number 重复检查
    serial_count: Dict[str, int] = defaultdict(int)

    for item in serial_items:
        serial = item.get("serial_number")
        if serial:
            serial_count[serial] += 1

    duplicated_serials = [
        serial for serial, count in serial_count.items()
        if count > 1
    ]

    if duplicated_serials:
        warnings.append(
            "发现重复 Serial Number，需要人工确认："
            + ", ".join(duplicated_serials)
        )

    # 3. 每行字段完整性检查
    for index, item in enumerate(serial_items, start=1):
        if not item.get("product_code"):
            warnings.append(f"第 {index} 行缺少产品编号。")

        if not item.get("serial_number"):
            warnings.append(f"第 {index} 行缺少 Serial Number。")

        if not item.get("expiration_date_iso"):
            warnings.append(
                f"第 {index} 行有效期无法解析："
                f"{item.get('expiration_date_raw')}"
            )

        delivered_qty = item.get("delivered_quantity")
        if delivered_qty is None:
            warnings.append(f"第 {index} 行缺少 Delivered 数量。")
        elif abs(delivered_qty - 1.0) > 1e-6:
            warnings.append(
                f"第 {index} 行 Delivered 数量不是 1.00："
                f"{delivered_qty}"
            )

    # 4. Total Completed 和实际提取数量的内部校验
    total_completed = doc_info.get("total_completed")

    if total_completed is not None:
        delivered_sum = sum(
            item.get("delivered_quantity") or 0
            for item in serial_items
        )

        if abs(total_completed - delivered_sum) > 1e-6:
            warnings.append(
                f"Total Completed = {total_completed}，"
                f"但提取到的 Delivered 数量总和 = {delivered_sum}。"
            )

    # 5. Total Demand 和 Total Completed 不一致时，不一定是错误。
    # 这可能表示工厂只确认了部分库存。
    # 所以这里给 warning，但不直接当成提取失败。
    total_demand = doc_info.get("total_demand")
    total_completed = doc_info.get("total_completed")

    if total_demand is not None and total_completed is not None:
        if abs(total_demand - total_completed) > 1e-6:
            warnings.append(
                f"工厂文件显示 Total Demand = {total_demand}，"
                f"Total Completed = {total_completed}。"
                f"这可能表示部分有货，需要在下一步和医院订单比对。"
            )

    return warnings


# ============================================================
# 8. 主提取函数
# ============================================================

def extract_factory_confirmation(pdf_path: Path) -> Dict[str, Any]:
    """
    主函数：
        读取工厂确认 PDF
        提取头信息
        提取 serial items
        汇总
        校验
        返回最终 result dict
    """
    print("[INFO] 读取工厂确认 PDF 文本...")
    pages_text, full_text = read_pdf_text(pdf_path)

    print("[INFO] 提取文件头信息...")
    header = extract_factory_header(full_text)

    print("[INFO] 提取 serial number 表格...")
    serial_items = extract_all_serial_items(pages_text)

    print("[INFO] 按产品编号汇总...")
    summary_by_product = summarize_by_product(serial_items)

    result: Dict[str, Any] = {
        "source_pdf": str(pdf_path),

        "factory_document": header,

        # 每一支产品的 serial 明细。
        # 后续生成发票或发货文件时，主要用这个。
        "serial_items": serial_items,

        # 按 product_code 汇总。
        # 后续和医院订单比对时，主要用这个。
        "summary_by_product": summary_by_product,

        "debug": {
            "page_count": len(pages_text),
            "pages_text_length": [
                {
                    "page": page["page"],
                    "text_length": len(page["text"]),
                }
                for page in pages_text
            ],
        },
    }

    warnings = validate_factory_result(result)
    result["warnings"] = warnings

    result["summary"] = {
        "warehouse_out": header.get("warehouse_out"),
        "bon_de_commande": header.get("bon_de_commande"),
        "shipping_date_raw": header.get("shipping_date_raw"),
        "shipping_date_iso": header.get("shipping_date_iso"),

        "serial_item_count": len(serial_items),
        "product_type_count": len(summary_by_product),

        "total_demand": header.get("total_demand"),
        "total_completed": header.get("total_completed"),

        "warning_count": len(warnings),
        "all_ok": len(warnings) == 0,
    }

    return result


# ============================================================
# 9. 可选：保存 PDF 原始文本，方便调试
# ============================================================

def save_raw_text_debug(pdf_path: Path, text_out_path: Path) -> None:
    """
    保存 PDF 提取出来的纯文本。

    用途：
        如果正则没有提取到某些字段，
        可以打开这个 txt 文件查看 PDF 文本到底长什么样。
    """
    pages_text, full_text = read_pdf_text(pdf_path)

    text_out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(text_out_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    print(f"[INFO] 原始文本已保存：{text_out_path}")


# ============================================================
# 10. 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract product serial numbers and expiration dates from factory confirmation PDF."
    )

    parser.add_argument(
        "--pdf",
        type=str,
        required=True,
        help="工厂确认 PDF 路径，例如 data/factory_confirmation.pdf",
    )

    parser.add_argument(
        "--out",
        type=str,
        required=True,
        help="输出 JSON 路径，例如 outputs/factory_confirmation.json",
    )

    parser.add_argument(
        "--save-text",
        type=str,
        default=None,
        help="可选：保存 PDF 解析出来的纯文本，方便调试。",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    out_path = Path(args.out)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 可选：保存原始文本，方便调试
    if args.save_text:
        save_raw_text_debug(
            pdf_path=pdf_path,
            text_out_path=Path(args.save_text),
        )

    # 提取工厂确认文件
    result = extract_factory_confirmation(pdf_path)

    # 保存 JSON
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n==============================")
    print("[DONE] 工厂确认文件提取完成")
    print("==============================")
    print(f"输出文件：{out_path}")
    print(f"WH/OUT: {result['summary']['warehouse_out']}")
    print(f"Bon de commande: {result['summary']['bon_de_commande']}")
    print(f"Shipping Date: {result['summary']['shipping_date_raw']}")
    print(f"Serial item count: {result['summary']['serial_item_count']}")
    print(f"Product type count: {result['summary']['product_type_count']}")
    print(f"Warnings: {result['summary']['warning_count']}")

    if result["warnings"]:
        print("\n需要人工确认的问题：")
        for warning in result["warnings"]:
            print(f"- {warning}")


if __name__ == "__main__":
    main()