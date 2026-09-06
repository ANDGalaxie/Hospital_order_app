# -*- coding: utf-8 -*-

"""
document_numbering.py

作用：
    为同一个医院订单统一生成 Invoice 编号和 Purchase Order 编号。

为什么需要这个文件：
    之前我们分别有：
        config/invoice_registry.json
        config/po_registry.json

    这样会导致同一个医院订单的 Invoice 和 PO 流水号不一致。

    现在改成统一使用：
        config/document_registry.json

    同一个 bon_de_commande 在同一个月份内只分配一次流水号。
    Invoice 和 PO 使用同一个流水号，只是编号格式不同。

编号规则：
    假设 document_date = 2026-06-02
    bon_de_commande = 150222
    这是 2026 年 6 月第 1 单。

    那么：

        shared_sequence = 01

        Invoice 编号：
            Invoice 20260106

        PO 编号：
            DELAHK0106S

    如果是同月第 2 单：

        Invoice 20260206
        DELAHK0206S

document_registry.json 结构：
    初始内容可以是：

        {}

    正式生成后会自动变成：

        {
          "2026-06": {
            "last_sequence": 1,
            "orders": {
              "150222": 1
            }
          }
        }

    含义：
        2026-06 这个月已经分配到第 1 单。
        bon_de_commande = 150222 对应流水号 1。

preview 模式：
    preview=True 时：
        - 只计算编号
        - 不更新 document_registry.json

正式模式：
    preview=False 时：
        - 如果订单已经存在，复用原来的流水号
        - 如果订单不存在，分配新流水号并更新 registry

注意：
    这个模块只负责编号和日期，不负责 PDF 生成。
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# ============================================================
# 1. 日期解析
# ============================================================

def parse_document_date(document_date_arg: Optional[str]) -> date:
    """
    解析 document_date。

    规则：
        如果用户传入 --document-date，则使用用户指定的日期。
        如果没有传入，则使用电脑本地日期。

    支持格式：
        2026-06-02
        2026-06-02 13:49:55

    返回：
        datetime.date 对象
    """
    if document_date_arg is None or str(document_date_arg).strip() == "":
        return datetime.now().date()

    text = str(document_date_arg).strip()

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    raise ValueError(
        f"无法解析 document_date：{document_date_arg}。"
        f"请使用 YYYY-MM-DD，例如 2026-06-02。"
    )


def format_date_fr_short(d: date) -> str:
    """
    发票中使用的法式短日期格式。

    例如：
        2026-06-02 -> 02-juin-26

    这个格式用于医院发票。
    """
    fr_months = {
        1: "janv.",
        2: "févr.",
        3: "mars",
        4: "avr.",
        5: "mai",
        6: "juin",
        7: "juil.",
        8: "août",
        9: "sept.",
        10: "oct.",
        11: "nov.",
        12: "déc.",
    }

    return f"{d.day:02d}-{fr_months[d.month]}-{d.year % 100:02d}"


def format_date_ddmmyyyy(d: date) -> str:
    """
    PO 中使用的日期格式：日/月/年。

    例如：
        2026-06-02 -> 02/06/2026
    """
    return d.strftime("%d/%m/%Y")


# ============================================================
# 2. Registry 读写
# ============================================================

def load_document_registry(registry_path: Path) -> Dict[str, Any]:
    """
    读取 document_registry.json。

    如果文件不存在，则返回空字典。

    允许初始内容：
        {}

    也兼容旧写法：
        {
          "2026-06": 0
        }

    如果发现旧写法，会自动在内存中转换成：
        {
          "2026-06": {
            "last_sequence": 0,
            "orders": {}
          }
        }
    """
    if not registry_path.exists():
        return {}

    with open(registry_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"{registry_path} 内容格式错误，顶层必须是 JSON object。"
        )

    normalized: Dict[str, Any] = {}

    for month_key, value in data.items():
        # 兼容旧格式：{"2026-06": 0}
        if isinstance(value, int):
            normalized[month_key] = {
                "last_sequence": int(value),
                "orders": {},
            }

        # 新格式：{"2026-06": {"last_sequence": 1, "orders": {...}}}
        elif isinstance(value, dict):
            last_sequence = int(value.get("last_sequence", 0))
            orders = value.get("orders", {})

            if not isinstance(orders, dict):
                orders = {}

            # 确保 orders 里的 sequence 都是 int
            normalized_orders = {
                str(order_key): int(seq)
                for order_key, seq in orders.items()
            }

            normalized[month_key] = {
                "last_sequence": last_sequence,
                "orders": normalized_orders,
            }

        else:
            raise ValueError(
                f"{registry_path} 中 {month_key} 的值格式错误。"
            )

    return normalized


def save_document_registry(
    registry: Dict[str, Any],
    registry_path: Path,
) -> None:
    """
    保存 document_registry.json。
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


# ============================================================
# 3. Bon de commande 标准化
# ============================================================

def normalize_bon_de_commande(value: Any) -> str:
    """
    标准化 bon_de_commande。

    例如：
        150222
        "150222"
        "BON DE COMMANDE N° 150222"

    最终尽量提取成：
        "150222"

    如果无法提取数字，则使用原始字符串。
    """
    if value is None:
        raise ValueError("bon_de_commande 为空，无法生成统一编号。")

    text = str(value).strip()

    if not text:
        raise ValueError("bon_de_commande 为空字符串，无法生成统一编号。")

    # 提取其中的数字
    digits = "".join(ch for ch in text if ch.isdigit())

    if digits:
        return digits

    return text


# ============================================================
# 4. 编号格式
# ============================================================

def build_invoice_number(
    document_date: date,
    sequence: int,
) -> str:
    """
    生成 Invoice 编号。

    规则：
        Invoice + 年份 + 两位流水号 + 两位月份

    例如：
        2026-06 第 1 单：
            Invoice 20260106

        2026-06 第 2 单：
            Invoice 20260206
    """
    return f"Invoice {document_date.year}{sequence:02d}{document_date.month:02d}"


def build_po_number(
    document_date: date,
    sequence: int,
) -> str:
    """
    生成 PO 编号。

    规则：
        DELAHK + 两位流水号 + 两位月份 + S

    例如：
        2026-06 第 1 单：
            DELAHK0106S

        2026-06 第 2 单：
            DELAHK0206S
    """
    return f"DELAHK{sequence:02d}{document_date.month:02d}S"


# ============================================================
# 5. 核心函数：获取同一订单的统一编号
# ============================================================

def get_document_numbers(
    bon_de_commande: Any,
    document_date: date,
    registry_path: Path,
    preview: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    获取同一个订单的统一编号。

    输入：
        bon_de_commande:
            医院订单号，例如 150222。

        document_date:
            生成文件日期。
            Invoice Date 和 PO Order Date 都应该来自这个日期。

        registry_path:
            config/document_registry.json

        preview:
            True  -> 不更新 registry
            False -> 正式写入 registry

    返回：
        numbers:
            {
              "sequence": 1,
              "invoice_number": "Invoice 20260106",
              "po_number": "DELAHK0106S",
              "month_key": "2026-06",
              "bon_de_commande": "150222",
              "is_existing_order": false,
              "preview": true
            }

        meta:
            用于 debug 的详细信息。
    """
    registry = load_document_registry(registry_path)

    bon = normalize_bon_de_commande(bon_de_commande)
    month_key = f"{document_date.year}-{document_date.month:02d}"

    # 如果当前月份还不存在，初始化。
    if month_key not in registry:
        registry[month_key] = {
            "last_sequence": 0,
            "orders": {},
        }

    month_data = registry[month_key]
    orders = month_data.setdefault("orders", {})
    last_sequence = int(month_data.get("last_sequence", 0))

    # 情况 1：这个 bon_de_commande 已经生成过编号
    if bon in orders:
        sequence = int(orders[bon])
        is_existing_order = True

    # 情况 2：这个 bon_de_commande 是当前月份的新订单
    else:
        sequence = last_sequence + 1
        is_existing_order = False

        # 正式模式才写入 registry。
        if not preview:
            orders[bon] = sequence
            month_data["last_sequence"] = sequence
            registry[month_key] = month_data
            save_document_registry(registry, registry_path)

    invoice_number = build_invoice_number(document_date, sequence)
    po_number = build_po_number(document_date, sequence)

    numbers = {
        "sequence": sequence,
        "invoice_number": invoice_number,
        "po_number": po_number,
        "month_key": month_key,
        "bon_de_commande": bon,
        "is_existing_order": is_existing_order,
        "preview": preview,
    }

    meta = {
        "registry_path": str(registry_path),
        "registry_after_operation": registry,
        "month_key": month_key,
        "last_sequence_before": last_sequence,
        "sequence_used": sequence,
        "is_existing_order": is_existing_order,
        "preview": preview,
    }

    return numbers, meta


# ============================================================
# 6. 从 extracted_order.json 中读取 Bon de commande
# ============================================================

def get_bon_de_commande_from_order_data(order_data: Dict[str, Any]) -> str:
    """
    从 extracted_order.json 中读取 bon_de_commande。

    目前 02_extract_order.py 的结构是：
        order_data["header"]["bon_de_commande"]

    这里单独封装，是为了两个脚本都能复用。
    """
    bon = (
        order_data.get("header", {}).get("bon_de_commande")
        or order_data.get("summary", {}).get("bon_de_commande")
    )

    return normalize_bon_de_commande(bon)


# ============================================================
# 7. 命令行测试入口
# ============================================================

if __name__ == "__main__":
    """
    这个文件通常被 05_generate_hospital_invoice.py
    和 06_generate_factory_po.py import 使用。

    但你也可以直接运行它测试编号逻辑：

        python src/document_numbering.py

    它会用测试数据：
        bon_de_commande = 150222
        document_date = 今天
        registry_path = config/document_registry.json
        preview = True

    注意：
        直接运行时默认 preview=True，不会修改 registry。
    """
    test_registry_path = Path("config/document_registry.json")
    test_document_date = datetime.now().date()
    test_bon = "150222"

    numbers, meta = get_document_numbers(
        bon_de_commande=test_bon,
        document_date=test_document_date,
        registry_path=test_registry_path,
        preview=True,
    )

    print("=== Document Numbering Preview Test ===")
    print(json.dumps(numbers, ensure_ascii=False, indent=2))