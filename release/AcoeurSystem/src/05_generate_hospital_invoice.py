# -*- coding: utf-8 -*-

"""
05_generate_hospital_invoice.py

作用：
    根据前面已经提取好的信息，自动生成发给医院的发票 PDF。

输入文件：
    1. outputs/extracted_order.json
       - 医院订单提取结果
       - 包含 bon_de_commande、shipping address、医院名称、下单产品等

    2. outputs/factory_confirmation.json
       - 工厂确认文件提取结果
       - 包含 shipping_date、serial number、expiration date、已确认产品数量等

    3. data/product_database.xlsx
       - 产品数据库
       - 用于查询产品描述和医院销售单价

    4. data/hospital_database.xlsx
       - 医院数据库
       - 用于查询医院账单地址 invoice address

    5. config/company_info.json
       - 公司固定信息
       - logo 路径、公司名称、地址、银行信息、payment terms 等

    6. config/document_registry.json
       - 统一文件编号流水记录
       - Invoice 和 Purchase Order 共用同一个订单流水号
       - 用于保证同一份医院订单生成的 Invoice 和 PO 编号一致

    7. templates/hospital_invoice.html
       - HTML 发票模板

输出文件：
    invoices/Invoice_20260106.html
    invoices/Invoice_20260106.pdf
    invoices/Invoice_20260106_data.json

主要逻辑：
    JSON + 产品数据库 + 医院数据库 + document_registry
    ↓
    根据“生成文件日期 document_date”统一生成 Invoice 编号
    ↓
    整理 invoice_data
    ↓
    Jinja2 填充 HTML 模板
    ↓
    WeasyPrint 转成 PDF

重要业务规则：
    1. Invoice Date 不再来自工厂 shipping date，而是来自生成文件日期 document_date。
    2. Invoice 编号不再使用独立 invoice_registry.json。
    3. Invoice 和发给工厂的 PO 共用 config/document_registry.json。
    4. 同一个 bon_de_commande 在同一个月份内复用同一个流水号。
"""

import argparse
import json
import re
import unicodedata

from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from rapidfuzz import fuzz, process
from weasyprint import HTML

# 统一编号模块：
# - Invoice 和 Purchase Order 共用同一个 document_registry.json
# - 同一个 bon_de_commande 会得到同一个月度流水号
# - 这样可以保证 Invoice 编号和 PO 编号只有前缀/格式不同，中间流水号一致
try:
    from document_numbering import (
        parse_document_date,
        format_date_fr_short,
        get_document_numbers,
    )
except ImportError:
    # 如果以后以 package 形式运行，例如 python -m src.05_generate_hospital_invoice，
    # 这个 fallback 可以避免导入失败。
    from src.document_numbering import (
        parse_document_date,
        format_date_fr_short,
        get_document_numbers,
    )


# ============================================================
# 1. 正则表达式与基础工具
# ============================================================

PRODUCT_RE = re.compile(
    r"BMA[-\s]*(\d)[\.,](\d{4})",
    re.IGNORECASE,
)


def normalize_product_code(text: str) -> Optional[str]:
    """
    标准化产品编号。

    输入可能是：
        BMA-2.5010
        BMA 2.5010
        BMA-2,5010

    输出统一为：
        BMA-2.5010

    如果没有识别到合法 BMA 编号，返回 None。
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


def normalize_match_text(text: str) -> str:
    """
    用于医院名称和地址匹配的标准化函数。

    作用：
        1. 去掉法语重音：
           Pôle -> Pole
           Hôpital -> Hopital

        2. 转大写

        3. 将标点符号替换为空格

        4. 合并多余空格

    这样可以让下面这些文本更容易匹配：
        CLINIQUE LOUIS PASTEUR Pôle87 ANGIOGRAPHIE
        Clinique Louis Pasteur
        7 Rue Parmentier
        7 RUE PARMENTIER
    """
    if text is None:
        return ""

    text = str(text)

    # 去掉重音
    text = unicodedata.normalize("NFD", text)
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) != "Mn"
    )

    # 转大写
    text = text.upper()

    # 常见标点转空格
    text = re.sub(r"[-_/(),.;:，]+", " ", text)

    # 合并多余空格
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def fix_address_display_text(text: str) -> str:
    """
    修正地址中常见 OCR 显示问题。

    这里只做非常保守的显示修正：
    - 不修改产品编号
    - 不修改金额
    - 不修改 serial number
    - 只修正常见地址/医院名称 OCR 问题

    目前主要修正：
        PÔle87 / POLE87 / Pole87 / Pôle87 -> Pôle87
    """
    if text is None:
        return ""

    text = str(text)

    # 修正 PÔle87 / POLE87 / Pole87 / POle87 -> Pôle87
    text = re.sub(
        r"\bP[ÔOÓÒÖôoóòö]LE\s*([0-9]+)\b",
        r"Pôle\1",
        text,
        flags=re.IGNORECASE,
    )

    return text


def format_address_display_line(text: str) -> str:
    """
    统一发票地址行的显示格式。

    作用：
        1. 去掉首尾空格；
        2. 合并多余空格；
        3. 将整行转为大写。

    为什么这样做：
        医院地址来自 OCR 或医院数据库时，可能出现大小写混合问题，例如：
            PÔle87 / Pôle87 / Pole87

        对地址类信息，商业单据中全部大写是可以接受且更稳定的。
        这样可以避免逐个识别法语重音和大小写。

    注意：
        这个函数只用于 Shipping Address 和 Invoice Address 的显示。
        不用于产品编号、金额、日期、serial number。
    """
    if text is None:
        return ""

    text = str(text).strip()
    text = re.sub(r"\s+", " ", text)

    return text.upper()


def format_address_display_lines(lines: List[str]) -> List[str]:
    """
    批量统一地址显示格式。

    输入：
        ["Clinique Louis Pasteur Pôle87", "87 Avenue ...", "France"]

    输出：
        ["CLINIQUE LOUIS PASTEUR PÔLE87", "87 AVENUE ...", "FRANCE"]
    """
    return [
        format_address_display_line(line)
        for line in lines
        if str(line).strip()
    ]



def parse_number(value: Any) -> Optional[float]:
    """
    把 Excel 或文本里的价格/数字转成 float。

    可以处理：
        270
        270.00
        "270.00 €"
        "270,00 €"
        "1 270,00 €"
    """
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    # 去掉欧元符号和空格
    text = text.replace("€", "")
    text = text.replace("\xa0", " ")
    text = text.replace(" ", "")

    # 如果既有逗号又有点，假设逗号是小数点，点是千分位的可能性低
    # 这里主要兼容法式 1 270,00
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        # 例如 1,270.00 或 1.270,00 很难完全自动判断
        # 当前项目中价格通常是 270.00 或 270,00
        # 简化处理：去掉逗号
        text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def format_eur(value: float) -> str:
    """
    将数字格式化成欧洲金额格式。

    输入：
        6480

    输出：
        6 480,00 €
    """
    value = round(float(value), 2)

    # 先生成英文格式：6,480.00
    text = f"{value:,.2f}"

    # 再转换成法式格式：6 480,00
    text = text.replace(",", "X").replace(".", ",").replace("X", " ")

    return f"{text} €"


def format_quantity(value: float) -> str:
    """
    第一页产品表中的数量显示。

    如果是整数，显示 3；
    如果不是整数，显示 3.50。
    """
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def format_serial_quantity(value: float) -> str:
    """
    第二页 serial number 表中的数量显示。

    样本中是 1.00，所以这里固定显示两位小数。
    """
    return f"{float(value):.2f}"


def product_code_to_default_description(product_code: str) -> str:
    """
    如果产品数据库中没有描述，则根据 BMA 编号生成一个默认描述。

    BMA-2.5010 -> 2.50 x 10 mm
    BMA-3.0040 -> 3.00 x 40 mm
    BMA-4.5020 -> 4.50 x 20 mm
    """
    code = normalize_product_code(product_code)
    if not code:
        return "HT Supreme™ Drug Eluting Stent"

    # code 形如 BMA-2.5010
    body = code.replace("BMA-", "")
    diameter_main, rest = body.split(".")
    diameter_decimal = rest[:2]
    length = rest[2:]

    diameter = f"{diameter_main}.{diameter_decimal}"

    return f"HT Supreme™ Drug Eluting Stent {diameter} x {int(length)} mm"


def sanitize_filename(text: str) -> str:
    """
    将发票编号转成适合作为文件名的形式。

    Invoice 20260106 -> Invoice_20260106
    """
    text = str(text).strip()
    text = re.sub(r"[^\w\-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


# ============================================================
# 2. 日期处理
# ============================================================

FR_MONTHS = {
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


def parse_iso_date(date_text: str) -> datetime:
    """
    解析 ISO 日期。

    支持：
        2026-06-01
        2026-06-01 13:49:55
    """
    if not date_text:
        raise ValueError("日期为空，无法解析。")

    date_text = str(date_text).strip()

    for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue

    raise ValueError(f"无法解析日期：{date_text}")


def format_date_fr(dt: datetime) -> str:
    """
    将日期格式化成发票显示格式。

    输入：
        2026-06-01

    输出：
        01-juin-26
    """
    month = FR_MONTHS[dt.month]
    return f"{dt.day:02d}-{month}-{dt.year % 100:02d}"


# ============================================================
# 3. JSON 读写
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
# 4. 旧版发票编号函数（保留但不再使用）
# ============================================================

def load_invoice_registry(path: Path) -> Dict[str, int]:
    """
    【旧版函数，当前主流程不再使用】

    以前 Invoice 使用独立的 invoice_registry.json。
    现在已经改为统一使用 config/document_registry.json，
    并通过 src/document_numbering.py 保证 Invoice 和 PO 共用同一个流水号。
    """
    if not path.exists():
        return {}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 确保 value 是 int
    return {str(k): int(v) for k, v in data.items()}


def save_invoice_registry(registry: Dict[str, int], path: Path) -> None:
    """
    保存发票流水记录。
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def generate_invoice_number(
    invoice_date: datetime,
    registry_path: Path,
    preview: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """
    【旧版函数，当前主流程不再使用】

    以前只给发票单独生成编号。
    现在统一编号逻辑已经移动到 document_numbering.py。
    """
    registry = load_invoice_registry(registry_path)

    key = f"{invoice_date.year}-{invoice_date.month:02d}"
    current_count = registry.get(key, 0)
    next_sequence = current_count + 1

    invoice_number = f"Invoice {invoice_date.year}{next_sequence:02d}{invoice_date.month:02d}"

    meta = {
        "registry_key": key,
        "current_count": current_count,
        "next_sequence": next_sequence,
        "preview": preview,
        "registry": registry,
    }

    return invoice_number, meta


def commit_invoice_number(meta: Dict[str, Any], registry_path: Path) -> None:
    """
    【旧版函数，当前主流程不再使用】

    现在正式编号提交由 document_numbering.get_document_numbers(..., preview=False) 完成。
    """
    registry = meta["registry"]
    key = meta["registry_key"]
    registry[key] = meta["next_sequence"]

    save_invoice_registry(registry, registry_path)


# ============================================================
# 5. 产品数据库读取
# ============================================================

def choose_price_from_row(row: pd.Series, columns: List[str]) -> Optional[float]:
    """
    从产品数据库的一行中选择医院销售单价。

    优先级：
        1. 列名中包含 Unit Price 和 DDP
        2. 列名中包含 hospital / invoice / price
        3. 行中第一个能解析出的数字价格

    这样写是为了兼容你现在的 Excel 格式。
    后面如果你把产品数据库整理成标准列名，代码会更稳定。
    """
    priority_cols = []

    for col in columns:
        simple = str(col).lower()
        if "unit" in simple and "price" in simple and "ddp" in simple:
            priority_cols.append(col)

    for col in columns:
        simple = str(col).lower()
        if any(k in simple for k in ["hospital", "invoice", "sale", "selling", "price"]):
            if col not in priority_cols:
                priority_cols.append(col)

    # 先按优先列找
    for col in priority_cols:
        value = parse_number(row.get(col))
        if value is not None:
            return value

    # 如果没有合适列名，就扫描整行
    # 但要避免把产品编号中的数字误认为价格。
    for col in columns:
        cell = row.get(col)
        value = parse_number(cell)

        if value is None:
            continue

        # 医院销售价一般不会是 1、2、3 这种小数字
        if value >= 10:
            return value

    return None


def extract_description_from_row(row: pd.Series, product_code: str) -> Optional[str]:
    """
    从产品数据库行中提取发票用产品描述。

    兼容几种情况：
        1. 有 invoice_description / Description 列
        2. 产品编号和描述在同一个单元格里
        3. 描述在其他文本列中
    """
    # 优先找列名明显的字段
    preferred_keywords = [
        "invoice_description",
        "description",
        "desc",
        "designation",
        "product_name",
        "name",
    ]

    for col in row.index:
        simple_col = str(col).lower()
        if any(k in simple_col for k in preferred_keywords):
            text = str(row.get(col, "")).strip()
            if not text or text.lower() == "nan":
                continue

            # 如果单元格里有产品编号，把产品编号去掉
            text = re.sub(PRODUCT_RE, "", text).strip()
            text = re.sub(r"\s+", " ", text)

            if text:
                return text

    # 如果找不到明显的 description 列，就扫描整行文本
    candidate_texts = []

    for col in row.index:
        text = str(row.get(col, "")).strip()
        if not text or text.lower() == "nan":
            continue

        # 优先选择含有 HT / Stent / Supreme 的文本
        simple = text.lower()
        if any(k in simple for k in ["stent", "supreme", "drug eluting", "ht"]):
            text = re.sub(PRODUCT_RE, "", text).strip()
            text = re.sub(r"\s+", " ", text)
            if text:
                candidate_texts.append(text)

    if candidate_texts:
        # 选择最长的那个作为描述
        return max(candidate_texts, key=len)

    return None


def load_product_catalog(product_db_path: Path) -> Dict[str, Dict[str, Any]]:
    """
    读取产品数据库，返回：
        {
            "BMA-2.5010": {
                "description": "...",
                "unit_price": 270.0,
                "raw_rows": [...]
            }
        }

    这个版本专门兼容你的产品数据库格式：

        Description                              Unit Price（DDP）
        BMA-2.5010
        HT Supreme™ Drug Eluting Stent ...       270,00 €
        BMA-2.5020
        HT Supreme™ Drug Eluting Stent ...       270,00 €

    也就是说：
        产品编号可能在一行；
        产品描述和价格可能在下一行。

    所以逻辑是：
        1. 扫描每一行，找到 BMA 产品编号；
        2. 从当前行 + 后面几行中提取描述和价格；
        3. 遇到下一个 BMA 编号时停止。
    """
    if not product_db_path.exists():
        raise FileNotFoundError(f"找不到产品数据库：{product_db_path}")

    catalog: Dict[str, Dict[str, Any]] = {}

    sheets = pd.read_excel(product_db_path, sheet_name=None, dtype=str)

    for sheet_name, df in sheets.items():
        df = df.fillna("")
        columns = list(df.columns)

        row_count = len(df)

        for row_index in range(row_count):
            row = df.iloc[row_index]

            # 当前行所有单元格合并成文本，用于查找 BMA 编号
            row_text = " ".join(str(row.get(col, "")) for col in columns)
            product_code = normalize_product_code(row_text)

            # 如果这一行没有产品编号，跳过
            if not product_code:
                continue

            # 收集当前产品相关的行：
            # 从当前行开始，往后看几行，直到遇到下一个 BMA 编号
            related_rows = []
            max_lookahead = 4

            for j in range(row_index, min(row_index + max_lookahead, row_count)):
                candidate_row = df.iloc[j]
                candidate_text = " ".join(str(candidate_row.get(col, "")) for col in columns)
                candidate_code = normalize_product_code(candidate_text)

                # 如果不是当前行，且遇到新的 BMA 产品编号，说明下一个产品开始了
                if j != row_index and candidate_code:
                    break

                related_rows.append(candidate_row)

            # 1. 提取产品描述
            description_candidates = []

            for r in related_rows:
                for col in columns:
                    cell = str(r.get(col, "")).strip()

                    if not cell or cell.lower() == "nan":
                        continue

                    # 去掉产品编号本身
                    cell_without_code = re.sub(PRODUCT_RE, "", cell).strip()
                    cell_without_code = re.sub(r"\s+", " ", cell_without_code)

                    # 产品描述通常包含这些词
                    simple = cell_without_code.lower()

                    if any(k in simple for k in ["stent", "supreme", "drug eluting", "ht"]):
                        description_candidates.append(cell_without_code)

            if description_candidates:
                # 通常最长的一条就是完整描述
                description = max(description_candidates, key=len)
            else:
                # 如果数据库里没找到描述，就用产品编号自动生成默认描述
                description = product_code_to_default_description(product_code)

            # 2. 提取价格
            unit_price = None

            # 优先从列名包含 price / ddp 的列找
            price_priority_cols = []

            for col in columns:
                simple_col = str(col).lower()

                if (
                    "price" in simple_col
                    or "ddp" in simple_col
                    or "prix" in simple_col
                    or "unit" in simple_col
                ):
                    price_priority_cols.append(col)

            # 先查价格列
            for r in related_rows:
                for col in price_priority_cols:
                    value = parse_number(r.get(col))

                    if value is not None and value >= 10:
                        unit_price = value
                        break

                if unit_price is not None:
                    break

            # 如果价格列没找到，再扫描所有列
            if unit_price is None:
                for r in related_rows:
                    for col in columns:
                        value = parse_number(r.get(col))

                        if value is not None and value >= 10:
                            unit_price = value
                            break

                    if unit_price is not None:
                        break

            catalog[product_code] = {
                "product_code": product_code,
                "description": description,
                "unit_price": unit_price,
                "source_sheet": sheet_name,
                "source_row_index": row_index + 1,
                "raw_rows": [r.to_dict() for r in related_rows],
            }

    return catalog


# ============================================================
# 6. 医院数据库读取与账单地址匹配
# ============================================================

def split_address_lines(address: str) -> List[str]:
    """
    把医院数据库中的地址拆成多行。

    如果 Excel 里本来有换行，就按换行拆。
    如果只有逗号，就按逗号拆。
    """
    if not address:
        return []

    text = str(address).strip()

    if "\n" in text:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
    elif "," in text:
        lines = [line.strip() for line in text.split(",") if line.strip()]
    else:
        lines = [text]

    # 去掉空行
    return [line for line in lines if line]


def load_hospital_database(hospital_db_path: Path) -> List[Dict[str, Any]]:
    """
    读取医院数据库。

    你的医院数据库有两列：
        医院名称
        账单地址

    代码会自动判断哪一列是医院名，哪一列是地址。
    """
    if not hospital_db_path.exists():
        raise FileNotFoundError(f"找不到医院数据库：{hospital_db_path}")

    suffix = hospital_db_path.suffix.lower()

    if suffix == ".csv":
        df = pd.read_csv(hospital_db_path, dtype=str)
    elif suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(hospital_db_path, dtype=str)
    else:
        raise ValueError("医院数据库请使用 .xlsx / .xls / .csv。")

    df = df.fillna("")
    columns = list(df.columns)

    if len(columns) < 2:
        raise ValueError("医院数据库至少需要两列：医院名称 + 账单地址。")

    # 尝试自动识别列
    name_col = None
    address_col = None

    for col in columns:
        simple = str(col).lower()
        if any(k in simple for k in ["hospital", "hopital", "hôpital", "clinique", "etablissement", "établissement", "name", "nom"]):
            name_col = col

        if any(k in simple for k in ["adresse", "address", "billing", "facturation"]):
            address_col = col

    # 如果列名不明显，就默认第一列医院名，第二列地址
    if name_col is None:
        name_col = columns[0]

    if address_col is None:
        address_col = columns[1]

    hospitals = []

    for _, row in df.iterrows():
        name = str(row.get(name_col, "")).strip()
        address = str(row.get(address_col, "")).strip()

        if not name:
            continue

        hospitals.append({
            "hospital_name": name,
            "billing_address_raw": address,
            "billing_address_lines": split_address_lines(address),
            "raw_row": row.to_dict(),
        })

    return hospitals


def find_invoice_address(
    order_data: Dict[str, Any],
    hospitals: List[Dict[str, Any]],
) -> Tuple[List[str], Dict[str, Any]]:
    """
    根据医院订单中提取到的 Adresse de la Facturation，
    在医院数据库中寻找最匹配的账单地址。

    旧逻辑：
        只用医院名称匹配数据库。

    新逻辑：
        同时使用：
            1. 订单中的账单医院名称
            2. 订单中的账单街道
            3. 订单中的账单邮编城市

        去匹配医院数据库中的：
            1. hospital_name
            2. billing_address

    为什么要这样：
        医院数据库里可能有很多相似名称。
        只用 Clinique Louis Pasteur 这种名字可能会匹配错。
        账单地址更能帮助定位唯一医院。
    """
    if not hospitals:
        raise ValueError("医院数据库为空，无法生成 Invoice Address。")

    addresses = order_data.get("addresses", {})

    billing_from_order = addresses.get("billing_address_from_order", {}) or {}
    shipping_from_order = addresses.get("shipping_address_from_order", {}) or {}

    # 订单中的账单名称，优先使用 Adresse de la Facturation 的 name_lines
    billing_name = " ".join(
        str(x).strip()
        for x in billing_from_order.get("name_lines", [])
        if str(x).strip()
    )

    # 如果账单名称没提取到，则兜底使用 hospital 字段
    if not billing_name:
        billing_name = (
            order_data.get("hospital", {}).get("billing_raw_name_from_order")
            or order_data.get("hospital", {}).get("matched_name_in_database")
            or order_data.get("hospital", {}).get("raw_name_from_order")
            or ""
        )

    # 再不行，用收货地址中的医院名称兜底
    if not billing_name:
        billing_name = " ".join(
            str(x).strip()
            for x in shipping_from_order.get("name_lines", [])
            if str(x).strip()
        )

    billing_street = billing_from_order.get("street") or ""
    billing_postal_city = billing_from_order.get("postal_city") or ""
    billing_phone = billing_from_order.get("phone") or ""
    billing_contact = billing_from_order.get("contact") or ""

    # 订单中提取出来的账单地址文本，用于和数据库地址匹配
    billing_address_query = " ".join(
        x for x in [
            billing_street,
            billing_postal_city,
            billing_phone,
            billing_contact,
        ]
        if x
    )

    if not billing_name and not billing_address_query:
        raise ValueError(
            "订单中没有提取到 Adresse de la Facturation，"
            "无法可靠匹配医院数据库。"
        )

    scored_candidates = []

    for hospital in hospitals:
        db_name = hospital.get("hospital_name", "")
        db_address_raw = hospital.get("billing_address_raw") or hospital.get("billing_address", "")
        db_address_lines = hospital.get("billing_address_lines", [])

        db_address_text = " ".join(
            [str(db_address_raw)] + [str(x) for x in db_address_lines]
        )

        name_score = fuzz.token_set_ratio(
            normalize_match_text(billing_name),
            normalize_match_text(db_name),
        )

        address_score = fuzz.token_set_ratio(
            normalize_match_text(billing_address_query),
            normalize_match_text(db_address_text),
        )

        # 如果订单中有账单地址，则地址权重更高。
        # 因为医院名称可能相似，但账单地址更唯一。
        if billing_address_query.strip():
            final_score = 0.40 * name_score + 0.60 * address_score
        else:
            final_score = name_score

        scored_candidates.append({
            "hospital": hospital,
            "hospital_name": db_name,
            "billing_address_raw": db_address_raw,
            "name_score": float(name_score),
            "address_score": float(address_score),
            "final_score": float(final_score),
        })

    scored_candidates.sort(key=lambda x: x["final_score"], reverse=True)

    best = scored_candidates[0]
    hospital = best["hospital"]

    top_candidates_debug = [
        {
            "hospital_name": c["hospital_name"],
            "billing_address": c["billing_address_raw"],
            "name_score": c["name_score"],
            "address_score": c["address_score"],
            "final_score": c["final_score"],
        }
        for c in scored_candidates[:5]
    ]

    # 阈值可以后续调整。
    # 这里用 80 是为了避免账单地址匹配错误。
    if best["final_score"] < 80:
        raise ValueError(
            "医院数据库匹配分数过低，需要人工确认：\n"
            f"billing_name_from_order={billing_name}\n"
            f"billing_address_from_order={billing_address_query}\n"
            f"best_match={best['hospital_name']}\n"
            f"name_score={best['name_score']}\n"
            f"address_score={best['address_score']}\n"
            f"final_score={best['final_score']}\n"
            f"top_candidates={top_candidates_debug}"
        )

    # 最终 Invoice Address 使用数据库中的标准账单地址
    lines = []

    hospital_name = hospital.get("hospital_name", "")
    if hospital_name:
        lines.append(hospital_name)

    billing_lines = hospital.get("billing_address_lines", [])
    if billing_lines:
        lines.extend(billing_lines)
    else:
        raw_address = (
            hospital.get("billing_address_raw")
            or hospital.get("billing_address")
            or ""
        )
        lines.extend(split_address_lines(raw_address))

    joined = " ".join(lines).lower()
    if "france" not in joined:
        lines.append("France")

    meta = {
        "billing_name_from_order": billing_name,
        "billing_address_from_order": billing_address_query,
        "matched_name": best["hospital_name"],
        "matched_billing_address": best["billing_address_raw"],
        "name_score": best["name_score"],
        "address_score": best["address_score"],
        "final_score": best["final_score"],
        "top_candidates": top_candidates_debug,
        "raw_row": hospital.get("raw_row"),
    }

    # 统一发票账单地址显示为大写。
    # 注意：这里只改变 PDF 显示，不影响前面的医院数据库匹配逻辑。
    lines = format_address_display_lines(lines)

    return lines, meta


# ============================================================
# 7. 从医院订单 JSON 整理 Shipping Address
# ============================================================

def build_shipping_address(order_data: Dict[str, Any]) -> List[str]:
    """
    从 extracted_order.json 中整理发票使用的 Shipping Address。

    重要修改：
        之前直接使用 display_lines。
        如果 JSON 中 name_lines 是：
            [
              "CLINIQUE LOUIS PASTEUR PÔle87",
              "ANGIOGRAPHIE"
            ]

        那么 PDF 就会自然显示成两行。

        现在改为优先使用结构化字段重新组装地址：
            1. name_lines 合并成一行
            2. street
            3. postal_city
            4. country
            5. phone
            6. fax
            7. contact

    这样医院名称和科室会显示在同一行：
        CLINIQUE LOUIS PASTEUR PÔLE87 ANGIOGRAPHIE

    同时所有地址显示统一大写，避免：
        PÔle87 / Pôle87 / Pole87
    大小写不一致的问题。
    """
    addresses = order_data.get("addresses", {})
    shipping = addresses.get("shipping_address_from_order", {}) or {}

    lines = []

    # ------------------------------------------------------------
    # 1. 优先使用结构化字段重新组装地址
    # ------------------------------------------------------------

    name_lines = shipping.get("name_lines", []) or []

    # 将医院名 + 科室合并成一行
    merged_name = " ".join(
        str(line).strip()
        for line in name_lines
        if str(line).strip()
    )

    if merged_name:
        lines.append(merged_name)

    street = shipping.get("street")
    postal_city = shipping.get("postal_city")
    country = shipping.get("country") or "France"

    if street:
        lines.append(str(street).strip())

    if postal_city:
        lines.append(str(postal_city).strip())

    if country:
        lines.append(str(country).strip())

    # 电话 / 传真 / 收件人
    # 注意：这里是发给医院的发票，所以可以保留完整收货信息。
    phone = shipping.get("phone")
    fax = shipping.get("fax")
    contact = shipping.get("contact")

    if phone:
        lines.append(f"Tél : {str(phone).strip()}")

    if fax:
        lines.append(f"Fax : {str(fax).strip()}")

    if contact:
        lines.append(f"Correspondant : {str(contact).strip()}")

    # 如果结构化字段已经足够，就直接返回
    if lines:
        joined = " ".join(lines).lower()
        if "france" not in joined:
            lines.append("France")

        return format_address_display_lines(lines)

    # ------------------------------------------------------------
    # 2. 如果结构化字段缺失，再兜底使用 display_lines
    # ------------------------------------------------------------

    display_lines = shipping.get("display_lines")
    if display_lines:
        lines = [
            str(line).strip()
            for line in display_lines
            if str(line).strip()
        ]

        joined = " ".join(lines).lower()
        if "france" not in joined:
            lines.append("France")

        return format_address_display_lines(lines)

    # ------------------------------------------------------------
    # 3. 再兜底使用 raw_delivery_lines
    # ------------------------------------------------------------

    raw_lines = addresses.get("raw_delivery_lines")
    if raw_lines:
        lines = [
            str(line).strip()
            for line in raw_lines
            if str(line).strip()
        ]

        joined = " ".join(lines).lower()
        if "france" not in joined:
            lines.append("France")

        return format_address_display_lines(lines)

    return []


# ============================================================
# 8. 根据工厂确认文件生成产品表和 Serial 表
# ============================================================

def get_order_product_sequence(order_data: Dict[str, Any]) -> List[str]:
    """
    从医院订单中提取产品顺序。

    发票第一页产品表最好按照医院订单中的顺序显示，
    而不是按照工厂确认文件中的顺序显示。
    """
    sequence = []

    for item in order_data.get("items", []):
        code = normalize_product_code(item.get("product_code") or item.get("raw_product_text") or "")
        if code and code not in sequence:
            sequence.append(code)

    return sequence


def build_factory_summary_map(factory_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    将 factory_confirmation.json 中的 summary_by_product 转成字典：
        {
            "BMA-2.5010": {...}
        }
    """
    result = {}

    for item in factory_data.get("summary_by_product", []):
        code = normalize_product_code(item.get("product_code", ""))
        if not code:
            continue

        result[code] = item

    return result


def build_invoice_items(
    order_data: Dict[str, Any],
    factory_data: Dict[str, Any],
    product_catalog: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    生成第一页产品汇总表 items。

    原则：
        发票只包含工厂确认文件中有 serial number 的产品。
        也就是只包含实际可发货的产品。

    但是显示顺序尽量按照医院订单的产品顺序。
    """
    warnings = []

    factory_map = build_factory_summary_map(factory_data)
    order_sequence = get_order_product_sequence(order_data)

    items = []

    used_codes = set()

    # 先按医院订单顺序生成
    for code in order_sequence:
        if code not in factory_map:
            continue

        confirmed = factory_map[code]
        quantity = float(confirmed.get("confirmed_quantity", 0) or 0)

        if quantity <= 0:
            continue

        product_info = product_catalog.get(code)

        if not product_info:
            warnings.append(f"产品数据库中找不到产品编号：{code}")
            description = product_code_to_default_description(code)
            unit_price = None
        else:
            description = product_info.get("description") or product_code_to_default_description(code)
            unit_price = product_info.get("unit_price")

        if unit_price is None:
            warnings.append(f"产品 {code} 缺少医院销售单价，无法计算金额。")
            amount = None
        else:
            amount = quantity * float(unit_price)

        items.append({
            "product_code": code,
            "description": description,
            "quantity_raw": quantity,
            "quantity": format_quantity(quantity),
            "unit_price_raw": unit_price,
            "unit_price": format_eur(unit_price) if unit_price is not None else "MISSING",
            "amount_raw": amount,
            "amount": format_eur(amount) if amount is not None else "MISSING",
        })

        used_codes.add(code)

    # 检查工厂文件中是否有医院订单里没有的产品
    extra_codes = [code for code in factory_map.keys() if code not in used_codes and code not in order_sequence]

    for code in extra_codes:
        warnings.append(
            f"工厂确认文件中出现医院订单里没有的产品：{code}。"
            f"该产品不会自动加入发票，需要人工确认。"
        )

    return items, warnings


def build_serial_items(factory_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    生成第二页 serial number 表。

    数据来自 factory_confirmation.json 的 serial_items。
    每一支产品一行。
    """
    serial_items = []

    for item in factory_data.get("serial_items", []):
        code = normalize_product_code(item.get("product_code", ""))
        if not code:
            continue

        qty = item.get("delivered_quantity", 1.0)
        if qty is None:
            qty = 1.0

        serial_items.append({
            "product_code": code,
            "quantity": format_serial_quantity(float(qty)),
            "serial_number": item.get("serial_number", ""),
            "expiration_date": item.get("expiration_date_iso"),
        })

    return serial_items


# ============================================================
# 9. 数据检查
# ============================================================

def validate_invoice_data(invoice_data: Dict[str, Any]) -> List[str]:
    """
    生成 PDF 前做检查。

    如果有严重问题，后面会停止生成。
    """
    errors = []

    invoice = invoice_data.get("invoice", {})
    addresses = invoice_data.get("addresses", {})
    items = invoice_data.get("items", [])
    serial_items = invoice_data.get("serial_items", [])

    if not invoice.get("invoice_number"):
        errors.append("缺少 invoice_number。")

    if not invoice.get("invoice_date"):
        errors.append("缺少 invoice_date。")

    if not invoice.get("due_date"):
        errors.append("缺少 due_date。")

    if not invoice.get("source"):
        errors.append("缺少 Source / Bon de commande。")

    if not addresses.get("shipping_address"):
        errors.append("缺少 Shipping Address。")

    if not addresses.get("invoice_address"):
        errors.append("缺少 Invoice Address。")

    if not items:
        errors.append("没有可生成发票的产品 items。")

    for item in items:
        if item.get("unit_price_raw") is None:
            errors.append(f"产品 {item.get('product_code')} 缺少单价。")

        if item.get("amount_raw") is None:
            errors.append(f"产品 {item.get('product_code')} 缺少金额。")

    if not serial_items:
        errors.append("没有 serial number 明细。")

    return errors


# ============================================================
# 10. 组装 invoice_data
# ============================================================

def build_invoice_data(
    order_data: Dict[str, Any],
    factory_data: Dict[str, Any],
    product_catalog: Dict[str, Dict[str, Any]],
    hospitals: List[Dict[str, Any]],
    company_info: Dict[str, Any],
    registry_path: Path,
    document_date: date,
    preview: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    将所有来源的数据组装成模板需要的 invoice_data。

    返回：
        invoice_data
        invoice_number_meta
        warnings
    """
    warnings = []

    # 1. 获取 Bon de commande
    bon_de_commande = (
        order_data.get("header", {}).get("bon_de_commande")
        or factory_data.get("factory_document", {}).get("bon_de_commande")
    )

    if not bon_de_commande:
        raise ValueError("无法找到 Bon de commande，不能生成发票。")

    # 2. Invoice Date 现在来自“生成文件日期 document_date”
    #
    # 重要：
    #   之前发票日期来自工厂确认文件里的 shipping date。
    #   现在根据你的业务要求，Invoice Date 和发给工厂的 PO Order Date
    #   都应该来自同一个“生成文件日期 document_date”。
    #
    #   这样同一份医院订单生成的两个文件：
    #       Invoice 20260106
    #       DELAHK0106S
    #   会共享同一个月度流水号 01。
    invoice_date = document_date

    payment_terms_days = int(company_info.get("payment_terms_days", 30))
    due_date = invoice_date + timedelta(days=payment_terms_days)

    # 3. 使用统一编号模块生成 Invoice 编号
    #
    # 这里故意使用 preview=True 只“计算编号”，不立即写入 registry。
    # 正式写入会放在 PDF 成功生成之后，避免 PDF 失败但编号被占用。
    document_numbers, document_number_meta = get_document_numbers(
        bon_de_commande=bon_de_commande,
        document_date=document_date,
        registry_path=registry_path,
        preview=True,
    )

    invoice_number = document_numbers["invoice_number"]

    # 4. 地址
    shipping_address = build_shipping_address(order_data)
    invoice_address, hospital_match_meta = find_invoice_address(order_data, hospitals)

    # 5. 产品表
    items, item_warnings = build_invoice_items(
        order_data=order_data,
        factory_data=factory_data,
        product_catalog=product_catalog,
    )
    warnings.extend(item_warnings)

    # 6. Serial Number 表
    serial_items = build_serial_items(factory_data)

    # 7. 金额汇总
    untaxed_amount = 0.0
    total_units = 0.0

    for item in items:
        amount = item.get("amount_raw")
        if amount is not None:
            untaxed_amount += float(amount)

        qty = item.get("quantity_raw")
        if qty is not None:
            total_units += float(qty)

    vat = 0.0
    total = untaxed_amount + vat

    # 8. 付款 reference
    payment_reference = f"{bon_de_commande}"

    invoice_data = {
        "invoice": {
            "invoice_number": invoice_number,
            "invoice_date": format_date_fr_short(invoice_date),
            "due_date": format_date_fr_short(due_date),
            "source": f"BON DE COMMANDE N° {bon_de_commande}",
            "payment_reference": payment_reference,
            "payment_terms_days": payment_terms_days,
        },

        "company": company_info,

        "addresses": {
            "shipping_address": shipping_address,
            "invoice_address": invoice_address,
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
            "bon_de_commande": bon_de_commande,
            "document_date_iso": document_date.isoformat(),
            "invoice_date_iso": invoice_date.isoformat(),
            "due_date_iso": due_date.isoformat(),
            "hospital_match": hospital_match_meta,
            "document_number_meta": document_number_meta,
            "document_numbers": document_numbers,
        },

        "warnings": warnings,
    }

    return invoice_data, document_number_meta, warnings


# ============================================================
# 11. HTML + PDF 生成
# ============================================================

def render_invoice_html(
    invoice_data: Dict[str, Any],
    template_dir: Path,
    template_name: str,
) -> str:
    """
    用 Jinja2 渲染 HTML 发票。
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    template = env.get_template(template_name)

    html_content = template.render(**invoice_data)

    return html_content


def write_html_and_pdf(
    html_content: str,
    html_path: Path,
    pdf_path: Path,
    project_root: Path,
) -> None:
    """
    保存 HTML，并转换成 PDF。

    注意：
        WeasyPrint 需要 base_url 才能找到 logo 图片。
        因为模板里写的是：
            data/logo.png

        所以 base_url 设置为项目根目录。
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
# 12. 主流程
# ============================================================

def generate_hospital_invoice(
    order_json_path: Path,
    factory_json_path: Path,
    product_db_path: Path,
    hospital_db_path: Path,
    company_info_path: Path,
    document_registry_path: Path,
    template_path: Path,
    invoices_dir: Path,
    project_root: Path,
    document_date_arg: Optional[str],
    preview: bool,
    allow_warnings: bool,
) -> Dict[str, Any]:
    """
    发票生成主函数。
    """
    print("[INFO] 读取医院订单 JSON...")
    order_data = load_json(order_json_path)

    print("[INFO] 读取工厂确认 JSON...")
    factory_data = load_json(factory_json_path)

    print("[INFO] 读取公司信息 company_info.json...")
    company_info = load_json(company_info_path)

    print("[INFO] 确定 Document Date...")
    document_date = parse_document_date(document_date_arg)
    print(f"[INFO] Document Date = {document_date.isoformat()}")

    print("[INFO] 读取产品数据库...")
    product_catalog = load_product_catalog(product_db_path)
    print(f"[INFO] 产品数据库产品数量：{len(product_catalog)}")

    print("[INFO] 读取医院数据库...")
    hospitals = load_hospital_database(hospital_db_path)
    print(f"[INFO] 医院数据库记录数量：{len(hospitals)}")

    print("[INFO] 组装 invoice_data...")
    invoice_data, document_number_meta, warnings = build_invoice_data(
        order_data=order_data,
        factory_data=factory_data,
        product_catalog=product_catalog,
        hospitals=hospitals,
        company_info=company_info,
        registry_path=document_registry_path,
        document_date=document_date,
        preview=preview,
    )

    print("[INFO] 检查 invoice_data...")
    errors = validate_invoice_data(invoice_data)

    if warnings:
        print("[WARNING] 发现非致命警告：")
        for w in warnings:
            print(f"  - {w}")

    if errors:
        print("[ERROR] 发现严重错误：")
        for e in errors:
            print(f"  - {e}")

        if not allow_warnings:
            raise RuntimeError(
                "发票数据存在严重错误，已停止生成。"
                "如果只是测试，可加 --allow-warnings。"
            )

    # 模板路径
    template_dir = template_path.parent
    template_name = template_path.name

    print("[INFO] 渲染 HTML 模板...")
    html_content = render_invoice_html(
        invoice_data=invoice_data,
        template_dir=template_dir,
        template_name=template_name,
    )

    invoice_number = invoice_data["invoice"]["invoice_number"]
    file_stem = sanitize_filename(invoice_number)

    html_path = invoices_dir / f"{file_stem}.html"
    pdf_path = invoices_dir / f"{file_stem}.pdf"
    data_path = invoices_dir / f"{file_stem}_data.json"

    print("[INFO] 生成 HTML 和 PDF...")
    write_html_and_pdf(
        html_content=html_content,
        html_path=html_path,
        pdf_path=pdf_path,
        project_root=project_root,
    )

    # 只有非 preview 模式，且 PDF 已经成功生成，才更新统一编号 registry。
    #
    # 注意：build_invoice_data 里只用 preview=True 计算了编号，
    # 没有真正写入 registry。这里 PDF 已成功生成后，才正式提交编号。
    if not preview:
        print("[INFO] 正式模式：更新统一 document registry...")
        bon_de_commande = invoice_data["debug"]["bon_de_commande"]
        committed_numbers, committed_meta = get_document_numbers(
            bon_de_commande=bon_de_commande,
            document_date=document_date,
            registry_path=document_registry_path,
            preview=False,
        )

        # 理论上这里应该与前面生成 PDF 使用的编号完全一致。
        # 如果不一致，说明 registry 在生成过程中被外部改动，需要人工检查。
        if committed_numbers["invoice_number"] != invoice_number:
            raise RuntimeError(
                "正式提交 registry 后得到的 Invoice 编号与 PDF 中的编号不一致："
                f"PDF={invoice_number}, registry={committed_numbers['invoice_number']}"
            )

        invoice_data["debug"]["document_number_commit_meta"] = committed_meta
        invoice_data["debug"]["document_numbers_committed"] = committed_numbers
    else:
        print("[INFO] Preview 模式：不更新统一 document registry。")

    print("[INFO] 保存发票数据 JSON...")
    save_json(invoice_data, data_path)

    result = {
        "invoice_number": invoice_number,
        "document_date": document_date.isoformat(),
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
        "data_path": str(data_path),
        "preview": preview,
        "warnings": warnings,
        "errors": errors,
    }

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generate hospital invoice HTML/PDF from extracted order and factory confirmation."
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
        "--products",
        type=str,
        default="data/product_database.xlsx",
        help="产品数据库 Excel。",
    )

    parser.add_argument(
        "--hospitals",
        type=str,
        default="data/hospital_database.xlsx",
        help="医院数据库 Excel/CSV。",
    )

    parser.add_argument(
        "--company",
        type=str,
        default="config/company_info.json",
        help="公司固定信息 JSON。",
    )

    parser.add_argument(
        "--registry",
        type=str,
        default="config/document_registry.json",
        help="统一文件编号流水记录 JSON。Invoice 和 PO 共用这个文件。",
    )

    parser.add_argument(
        "--document-date",
        type=str,
        default=None,
        help=(
            "可选：手动指定生成文件日期，格式 YYYY-MM-DD。"
            "如果不传，则使用电脑本地日期。"
            "Invoice Date、Due Date、Invoice 编号月份都基于这个日期。"
        ),
    )

    parser.add_argument(
        "--template",
        type=str,
        default="templates/hospital_invoice.html",
        help="发票 HTML 模板。",
    )

    parser.add_argument(
        "--out-dir",
        type=str,
        default="invoices",
        help="发票输出文件夹。",
    )

    parser.add_argument(
        "--preview",
        action="store_true",
        help="预览模式：生成 HTML/PDF，但不更新发票编号流水。",
    )

    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="即使有严重错误也继续生成，用于调试模板。不建议正式使用。",
    )

    args = parser.parse_args()

    project_root = Path.cwd()

    result = generate_hospital_invoice(
        order_json_path=Path(args.order),
        factory_json_path=Path(args.factory),
        product_db_path=Path(args.products),
        hospital_db_path=Path(args.hospitals),
        company_info_path=Path(args.company),
        document_registry_path=Path(args.registry),
        template_path=Path(args.template),
        invoices_dir=Path(args.out_dir),
        project_root=project_root,
        document_date_arg=args.document_date,
        preview=args.preview,
        allow_warnings=args.allow_warnings,
    )

    print("\n==============================")
    print("[DONE] 医院发票生成完成")
    print("==============================")
    print(f"Invoice Number: {result['invoice_number']}")
    print(f"Document Date: {result['document_date']}")
    print(f"HTML: {result['html_path']}")
    print(f"PDF: {result['pdf_path']}")
    print(f"Data JSON: {result['data_path']}")
    print(f"Preview mode: {result['preview']}")

    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"- {w}")

    if result["errors"]:
        print("\nErrors:")
        for e in result["errors"]:
            print(f"- {e}")


if __name__ == "__main__":
    main()