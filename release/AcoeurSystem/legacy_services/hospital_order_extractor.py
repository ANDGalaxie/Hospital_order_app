# -*- coding: utf-8 -*-

"""
02_extract_order.py

作用：
    从医院发来的订单 PDF 中提取结构化订单信息。

输入：
    1. 医院订单 PDF
    2. 产品数据库 Excel
    3. 医院数据库 Excel / CSV，可选

输出：
    outputs/extracted_order.json

当前功能：
    1. PDF 转图片
    2. PaddleOCR 法语识别
    3. 提取 Bon de commande
    4. 提取 Date de commande
    5. 提取 Adresse de la Livraison
       - 医院名称 / 科室
       - 收货地址
       - 电话
       - 传真
       - 收货联系人
    6. 提取 Adresse de la Facturation
       - 医院名称
       - 账单地址
       - 电话
       - 传真
       - 账单联系人
    7. 提取产品编号 BMA-xxxx
    8. 提取 Boît 数量
    9. 提取 Compte 小列数字
    10. 校验 Boît 数量和 Compte 数量是否一致
    11. 用产品数据库校验产品编号
    12. 可选：用医院名称 + 账单地址匹配医院数据库
"""

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
from PIL import Image
from rapidfuzz import fuzz, process


# ============================================================
# 1. 正则表达式
# ============================================================

PRODUCT_RE = re.compile(
    r"BMA[-\s]*(\d)[\.,](\d{4})",
    re.IGNORECASE,
)

DATE_RE = re.compile(
    r"(\d{2}/\d{2}/\d{4})"
)

BON_RE = re.compile(
    r"BON\s+DE\s+COMMANDE\s*N[°o]?\s*([0-9]+)",
    re.IGNORECASE,
)

POSTAL_CITY_RE = re.compile(
    r"\b(\d{5})\b\s+(.+)"
)

COMPTE_QTY_RE = re.compile(
    r"^\d{1,3}$"
)


# ============================================================
# 2. 通用文本工具函数
# ============================================================

def strip_accents(text: str) -> str:
    """
    去掉法语重音。

    例：
        Boît -> Boit
        Référence -> Reference
        Pôle -> Pole
    """
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text


def simplify_text(text: str) -> str:
    """
    用于规则判断的简化文本：
        - 去重音
        - 转大写
        - 合并多余空格
    """
    text = strip_accents(str(text))
    text = text.upper()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_match_text(text: str) -> str:
    """
    用于医院名称和地址匹配的文本标准化。

    目的：
        避免大小写、重音、标点差异导致匹配失败。
    """
    text = strip_accents(str(text))
    text = text.upper()
    text = re.sub(r"[-_/(),.;:，]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_line(text: str) -> str:
    """
    清理 OCR 文本行。
    """
    text = str(text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_product_code(text: str) -> Optional[str]:
    """
    从 OCR 文本中提取标准 BMA 产品编号。

    输入可能是：
        BMA-2.5020
        BMA 2.5020
        BMA-2,5020

    输出：
        BMA-2.5020
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


def aggressive_product_candidate(text: str) -> Optional[str]:
    """
    生成一个疑似修正后的产品编号。

    注意：
        只用于人工确认建议。
        不能自动替换后直接生成正式文件。
    """
    if not text:
        return None

    text = str(text).upper()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(",", ".")

    if "BMA" not in text:
        return None

    text = text[text.find("BMA"):]

    # 常见 OCR 错误
    text = text.replace("O", "0")
    text = text.replace("I", "1")
    text = text.replace("L", "1")

    return normalize_product_code(text)


def extract_boit_quantity(text: str) -> Optional[int]:
    """
    从 “3 Boît” / “2 Boit” 中提取数量。
    """
    simple = simplify_text(text)
    match = re.search(r"\b(\d{1,3})\s*BOIT\b", simple)

    if not match:
        return None

    return int(match.group(1))


def extract_compte_quantity(text: str) -> Optional[int]:
    """
    从 Compte 小列中提取单独数字。
    """
    text = str(text).strip()

    if not COMPTE_QTY_RE.fullmatch(text):
        return None

    return int(text)


def is_street_line(text: str) -> bool:
    """
    判断一行是否像法国街道地址。

    例：
        87 AVENUE DU 69EME REGIMENT D'INFANTERIE
        7 RUE PARMENTIER
    """
    simple = simplify_text(text)

    street_keywords = [
        "RUE",
        "AVENUE",
        "AV",
        "BD",
        "BOULEVARD",
        "ROUTE",
        "PLACE",
        "CHEMIN",
        "IMPASSE",
        "ALLEE",
        "QUAI",
        "COURS",
        "ZAC",
    ]

    starts_with_number = re.match(r"^\d+", simple) is not None
    has_street_keyword = any(k in simple for k in street_keywords)

    return starts_with_number and has_street_keyword


def is_phone_line(text: str) -> bool:
    """
    判断是否为电话行。
    """
    simple = simplify_text(text)
    return simple.startswith("TEL") or simple.startswith("TÉL")


def is_fax_line(text: str) -> bool:
    """
    判断是否为传真行。
    """
    simple = simplify_text(text)
    return simple.startswith("FAX")


def is_contact_line(text: str) -> bool:
    """
    判断是否为联系人行。
    """
    simple = simplify_text(text)
    return simple.startswith("CORRESPONDANT")


def extract_value_after_colon(text: str) -> Optional[str]:
    """
    从类似：
        Tél : 03.83.18.86.77
        Correspondant : Mme Pauline DALBIES

    中提取冒号后面的值。
    """
    if ":" in text:
        value = text.split(":", 1)[1].strip()
    elif "：" in text:
        value = text.split("：", 1)[1].strip()
    else:
        value = ""

    return value if value else None


def build_display_lines(address: Dict[str, Any]) -> List[str]:
    """
    根据结构化地址信息生成发票可直接显示的多行地址。
    """
    lines: List[str] = []

    for line in address.get("name_lines", []):
        if line:
            lines.append(line)

    if address.get("street"):
        lines.append(address["street"])

    if address.get("postal_city"):
        lines.append(address["postal_city"])

    if address.get("country"):
        lines.append(address["country"])

    if address.get("phone"):
        lines.append(f"Tél: {address['phone']}")

    if address.get("fax"):
        lines.append(f"Fax: {address['fax']}")

    if address.get("contact"):
        lines.append(f"Correspondant: {address['contact']}")

    return lines


# ============================================================
# 3. PDF OCR
# ============================================================

def pdf_to_images(pdf_path: Path, out_dir: Path, dpi: int = 220) -> List[Path]:
    """
    把 PDF 每一页转换成 PNG 图片。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    image_paths: List[Path] = []

    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)

    for page_index in range(len(doc)):
        page = doc[page_index]
        pix = page.get_pixmap(matrix=mat, alpha=False)

        image_path = out_dir / f"page_{page_index + 1}.png"
        pix.save(image_path)

        image_paths.append(image_path)

    return image_paths


def run_paddleocr_for_pdf(
    pdf_path: Path,
    ocr_dir: Path,
    lang: str = "fr",
    force_ocr: bool = False,
) -> None:
    """
    对 PDF 执行 PaddleOCR。

    如果 ocr_dir 已经有 OCR JSON，默认不重复 OCR。
    如果加 --force-ocr，则强制重新 OCR。
    """
    existing_jsons = list(ocr_dir.glob("*_res.json"))

    if existing_jsons and not force_ocr:
        print(f"[INFO] 已发现 OCR JSON，跳过重新 OCR：{ocr_dir}")
        return

    print("[INFO] PDF 转图片...")
    image_paths = pdf_to_images(pdf_path, ocr_dir)

    print("[INFO] 加载 PaddleOCR...")
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(
        lang=lang,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )

    print("[INFO] 开始 OCR...")
    for image_path in image_paths:
        print(f"[INFO] OCR: {image_path}")

        result = ocr.predict(str(image_path))

        for res in result:
            res.save_to_json(save_path=str(ocr_dir))
            res.save_to_img(save_path=str(ocr_dir))

    print(f"[INFO] OCR 完成，结果保存在：{ocr_dir}")


# ============================================================
# 4. 读取 OCR JSON
# ============================================================

def box_to_xyxy(box: Any) -> Tuple[float, float, float, float]:
    """
    将 OCR box 转成 x1, y1, x2, y2。

    兼容：
        [x1, y1, x2, y2]
    和：
        [[x,y], [x,y], [x,y], [x,y]]
    """
    if (
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(v, (int, float)) for v in box)
    ):
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])

    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]

    return min(xs), min(ys), max(xs), max(ys)


def find_ocr_json_files(ocr_dir: Path) -> List[Path]:
    """
    找到 OCR JSON 文件。
    """
    json_files = sorted(ocr_dir.glob("*_res.json"))

    if not json_files:
        raise FileNotFoundError(
            f"没有在 {ocr_dir} 找到 *_res.json。请先运行 OCR。"
        )

    return json_files


def infer_page_number(json_path: Path, data: Dict[str, Any]) -> int:
    """
    从文件名或 JSON 字段推断页码。
    """
    match = re.search(r"page[_-]?(\d+)", json_path.name, re.IGNORECASE)
    if match:
        return int(match.group(1))

    page_index = data.get("page_index")
    if isinstance(page_index, int):
        return page_index + 1

    return 1


def resolve_image_size(json_path: Path, data: Dict[str, Any]) -> Tuple[int, int]:
    """
    获取 OCR 输入图片尺寸。
    """
    input_path = data.get("input_path")

    if input_path:
        img_path = Path(input_path)

        if not img_path.exists():
            img_path = json_path.parent / Path(input_path).name

        if img_path.exists():
            with Image.open(img_path) as img:
                return img.size

    boxes = data.get("rec_boxes", [])
    max_x = 1
    max_y = 1

    for box in boxes:
        x1, y1, x2, y2 = box_to_xyxy(box)
        max_x = max(max_x, x2)
        max_y = max(max_y, y2)

    return int(max_x + 100), int(max_y + 100)


def load_ocr_blocks(ocr_dir: Path) -> List[Dict[str, Any]]:
    """
    读取所有 OCR JSON，转换成 text blocks。

    每个 block 包含：
        text
        score
        box
        x_center
        y_center
        page
        page_width
        page_height
    """
    blocks: List[Dict[str, Any]] = []

    json_files = find_ocr_json_files(ocr_dir)

    for json_path in json_files:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        page_number = infer_page_number(json_path, data)
        page_width, page_height = resolve_image_size(json_path, data)

        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        boxes = data.get("rec_boxes", [])

        n = min(len(texts), len(scores), len(boxes))

        for i in range(n):
            text = clean_line(texts[i])
            if not text:
                continue

            x1, y1, x2, y2 = box_to_xyxy(boxes[i])

            blocks.append({
                "text": text,
                "score": float(scores[i]),
                "box": [x1, y1, x2, y2],
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "x_center": (x1 + x2) / 2,
                "y_center": (y1 + y2) / 2,
                "page": page_number,
                "page_width": page_width,
                "page_height": page_height,
                "source_json": str(json_path),
            })

    return blocks


# ============================================================
# 5. 产品数据库校验
# ============================================================

def load_product_codes(product_db_path: Path) -> List[str]:
    """
    从产品数据库中读取所有合法 BMA 编号。

    为了兼容不同 Excel 格式，这里扫描所有 sheet 和所有单元格。
    """
    if not product_db_path.exists():
        raise FileNotFoundError(f"找不到产品数据库：{product_db_path}")

    product_codes = set()

    sheets = pd.read_excel(product_db_path, sheet_name=None, dtype=str)

    for _, df in sheets.items():
        df = df.fillna("")

        for col in df.columns:
            for value in df[col].astype(str):
                code = normalize_product_code(value)
                if code:
                    product_codes.add(code)

    return sorted(product_codes)


def validate_product_code(raw_text: str, product_codes: List[str]) -> Dict[str, Any]:
    """
    校验产品编号是否存在于产品数据库。
    """
    code = normalize_product_code(raw_text)

    if code and code in product_codes:
        return {
            "raw_product_text": raw_text,
            "product_code": code,
            "product_status": "OK",
            "product_suggestions": [],
        }

    candidate = aggressive_product_candidate(raw_text)
    search_key = candidate if candidate else raw_text

    suggestions = []

    if product_codes:
        matches = process.extract(
            search_key,
            product_codes,
            scorer=fuzz.ratio,
            limit=3,
        )

        suggestions = [
            {
                "code": item[0],
                "score": float(item[1]),
            }
            for item in matches
        ]

    return {
        "raw_product_text": raw_text,
        "product_code": code,
        "product_status": "NEEDS_REVIEW",
        "product_suggestions": suggestions,
    }


# ============================================================
# 6. 医院数据库读取与匹配
# ============================================================

def load_hospital_database(hospital_db_path: Optional[Path]) -> List[Dict[str, Any]]:
    """
    读取医院数据库。

    允许字段：
        hospital_name / Etablissement / Nom
        billing_address / Adresse / Facturation

    如果医院数据库只有两列，则默认：
        第一列 = 医院名称
        第二列 = 账单地址
    """
    if hospital_db_path is None:
        return []

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

    if not columns:
        return []

    name_col = None
    address_col = None

    for col in columns:
        simple = normalize_match_text(col)

        if any(k in simple for k in ["ETABLISSEMENT", "HOSPITAL", "HOPITAL", "CLINIQUE", "NAME", "NOM"]):
            name_col = col

        if any(k in simple for k in ["ADRESSE", "ADDRESS", "BILLING", "FACTURATION"]):
            address_col = col

    if name_col is None:
        name_col = columns[0]

    if address_col is None:
        address_col = columns[1] if len(columns) >= 2 else columns[0]

    hospitals: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        name = clean_line(row.get(name_col, ""))
        address = clean_line(row.get(address_col, ""))

        if not name:
            continue

        hospitals.append({
            "hospital_name": name,
            "billing_address": address,
            "raw_row": row.to_dict(),
        })

    return hospitals


def score_hospital_candidate(
    shipping_address: Dict[str, Any],
    billing_address: Dict[str, Any],
    hospital: Dict[str, Any],
) -> Dict[str, Any]:
    """
    对某一条医院数据库记录打分。

    新逻辑：
        不只用医院名称匹配，
        还使用订单中提取出的 Adresse de la Facturation 进行匹配。

    为什么：
        医院数据库里可能有很多名字相近的医院。
        账单地址通常更能唯一定位医院。
    """
    order_shipping_name = " ".join(shipping_address.get("name_lines", []))
    order_billing_name = " ".join(billing_address.get("name_lines", []))

    order_billing_addr_parts = [
        billing_address.get("street") or "",
        billing_address.get("postal_city") or "",
    ]

    order_billing_addr = " ".join(order_billing_addr_parts)

    db_name = hospital.get("hospital_name", "")
    db_addr = hospital.get("billing_address", "")

    # 名称 query：优先使用账单地址中的医院名称
    query_name = order_billing_name or order_shipping_name

    name_score = fuzz.token_set_ratio(
        normalize_match_text(query_name),
        normalize_match_text(db_name),
    )

    # 地址 score：使用订单中的账单地址 vs 数据库账单地址
    address_score = fuzz.token_set_ratio(
        normalize_match_text(order_billing_addr),
        normalize_match_text(db_addr),
    )

    # 如果订单里没有提取到账单地址，则只能靠名称
    if order_billing_addr.strip():
        final_score = 0.40 * name_score + 0.60 * address_score
    else:
        final_score = name_score

    return {
        "hospital_name": db_name,
        "billing_address": db_addr,
        "name_score": float(name_score),
        "address_score": float(address_score),
        "final_score": float(final_score),
        "raw_row": hospital.get("raw_row"),
    }


def match_hospital(
    shipping_address: Dict[str, Any],
    billing_address: Dict[str, Any],
    hospitals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    用医院名称 + 账单地址匹配医院数据库。
    """
    if not hospitals:
        return {
            "raw_name_from_order": " ".join(shipping_address.get("name_lines", [])),
            "billing_raw_name_from_order": " ".join(billing_address.get("name_lines", [])),
            "matched_name_in_database": None,
            "match_score": None,
            "match_status": "SKIPPED",
            "billing_address_from_database": None,
            "message": "未提供医院数据库，跳过医院匹配。",
        }

    scored = [
        score_hospital_candidate(shipping_address, billing_address, h)
        for h in hospitals
    ]

    scored = sorted(scored, key=lambda x: x["final_score"], reverse=True)

    best = scored[0]
    top_candidates = scored[:5]

    status = "OK" if best["final_score"] >= 80 else "NEEDS_REVIEW"

    return {
        "raw_name_from_order": " ".join(shipping_address.get("name_lines", [])),
        "billing_raw_name_from_order": " ".join(billing_address.get("name_lines", [])),
        "billing_address_from_order_for_matching": {
            "street": billing_address.get("street"),
            "postal_city": billing_address.get("postal_city"),
            "phone": billing_address.get("phone"),
            "contact": billing_address.get("contact"),
        },

        "matched_name_in_database": best["hospital_name"],
        "billing_address_from_database": best["billing_address"],
        "match_score": best["final_score"],
        "name_score": best["name_score"],
        "address_score": best["address_score"],
        "match_status": status,
        "top_candidates": top_candidates,
        "matched_raw_row": best["raw_row"],
    }


# ============================================================
# 7. 提取订单头
# ============================================================

def extract_header(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    提取 Bon de commande 和 Date de commande。
    """
    sorted_blocks = sorted(
        blocks,
        key=lambda b: (b["page"], b["y_center"], b["x_center"]),
    )

    all_text = "\n".join(b["text"] for b in sorted_blocks)

    bon_match = BON_RE.search(all_text)
    date_match = None

    for block in sorted_blocks:
        simple = simplify_text(block["text"])
        if "DATE DE COMMANDE" in simple:
            date_match = DATE_RE.search(block["text"])
            if date_match:
                break

    if not date_match:
        date_match = DATE_RE.search(all_text)

    return {
        "bon_de_commande": bon_match.group(1) if bon_match else None,
        "order_date": date_match.group(1) if date_match else None,
    }


# ============================================================
# 8. 地址区域提取
# ============================================================

def find_first_label_block(
    blocks: List[Dict[str, Any]],
    label_keywords: List[str],
    x_max_ratio: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    在第一页 OCR blocks 中寻找某个标签块。

    label_keywords 使用简化后的关键字，例如：
        ADRESSE DE LA LIVRAISON
        ADRESSE DE LA FACTURATION
    """
    for block in sorted(blocks, key=lambda b: (b["y_center"], b["x_center"])):
        page_width = block["page_width"]

        if x_max_ratio is not None and block["x_center"] > page_width * x_max_ratio:
            continue

        simple = simplify_text(block["text"])

        for keyword in label_keywords:
            if keyword in simple:
                return block

    return None


def extract_address_section(
    blocks: List[Dict[str, Any]],
    start_keywords: List[str],
    end_keywords: List[str],
    fallback_y_min_ratio: float,
    fallback_y_max_ratio: float,
    section_name: str,
) -> Dict[str, Any]:
    """
    通用地址提取函数。

    用于提取：
        - Adresse de la Livraison
        - Adresse de la Facturation

    逻辑：
        1. 找 start label
        2. 找 end label
        3. 只取左侧区域
        4. 分类提取：
            - name_lines
            - street
            - postal_city
            - phone
            - fax
            - contact
    """
    page1_blocks = [b for b in blocks if b["page"] == 1]

    if not page1_blocks:
        return {
            "section_name": section_name,
            "status": "NEEDS_REVIEW",
            "message": "没有找到第一页 OCR 内容。",
            "raw_lines": [],
        }

    page_width = page1_blocks[0]["page_width"]
    page_height = page1_blocks[0]["page_height"]

    # 医院的两个地址区域都在左侧，所以限制左半部分
    left_x_max_ratio = 0.55

    start_block = find_first_label_block(
        page1_blocks,
        start_keywords,
        x_max_ratio=left_x_max_ratio,
    )

    if start_block:
        y_min = start_block["y2"]
    else:
        y_min = page_height * fallback_y_min_ratio

    # end label 也优先在左侧找。
    # 对 billing section，end 可能是整行 livraison note 或 table header。
    end_block = find_first_label_block(
        [
            b for b in page1_blocks
            if b["y_center"] > y_min
        ],
        end_keywords,
        x_max_ratio=None,
    )

    if end_block:
        y_max = end_block["y1"]
    else:
        y_max = page_height * fallback_y_max_ratio

    candidates = [
        b for b in page1_blocks
        if b["x_center"] < page_width * left_x_max_ratio
        and y_min < b["y_center"] < y_max
    ]

    candidates = sorted(candidates, key=lambda b: (b["y_center"], b["x_center"]))

    raw_lines: List[str] = []

    for block in candidates:
        text = clean_line(block["text"])
        simple = simplify_text(text)

        # 排除地址区域标题本身
        if any(keyword in simple for keyword in start_keywords):
            continue

        if any(keyword in simple for keyword in end_keywords):
            continue

        if text:
            raw_lines.append(text)

    # 分类提取电话、传真、联系人
    phone = None
    fax = None
    contact = None

    non_contact_lines: List[str] = []

    for line in raw_lines:
        if is_phone_line(line):
            phone = extract_value_after_colon(line)
        elif is_fax_line(line):
            fax = extract_value_after_colon(line)
        elif is_contact_line(line):
            contact = extract_value_after_colon(line)
        else:
            non_contact_lines.append(line)

    # 在剩余行中找街道
    street_index = None
    for i, line in enumerate(non_contact_lines):
        if is_street_line(line):
            street_index = i
            break

    postal_index = None
    if street_index is not None:
        for i in range(street_index + 1, len(non_contact_lines)):
            if POSTAL_CITY_RE.search(non_contact_lines[i]):
                postal_index = i
                break

    if street_index is None:
        address = {
            "section_name": section_name,
            "name_lines": non_contact_lines,
            "street": None,
            "postal_city": None,
            "phone": phone,
            "fax": fax,
            "contact": contact,
            "country": "France",
            "display_lines": [],
            "raw_lines": raw_lines,
            "status": "NEEDS_REVIEW",
            "message": f"{section_name}: 没有识别到街道行。",
        }
        address["display_lines"] = build_display_lines(address)
        return address

    name_lines = non_contact_lines[:street_index]
    street = non_contact_lines[street_index]
    postal_city = non_contact_lines[postal_index] if postal_index is not None else None

    status = "OK"
    message = None

    if not name_lines:
        status = "NEEDS_REVIEW"
        message = f"{section_name}: 没有识别到医院名称。"

    if not street or not postal_city:
        status = "NEEDS_REVIEW"
        message = f"{section_name}: 地址不完整，缺少街道或邮编城市。"

    address = {
        "section_name": section_name,
        "name_lines": name_lines,
        "street": street,
        "postal_city": postal_city,
        "phone": phone,
        "fax": fax,
        "contact": contact,
        "country": "France",
        "display_lines": [],
        "raw_lines": raw_lines,
        "status": status,
        "message": message,
    }

    address["display_lines"] = build_display_lines(address)

    return address


def extract_addresses(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    同时提取收货地址和账单地址。
    """
    shipping_address = extract_address_section(
        blocks=blocks,
        start_keywords=["ADRESSE DE LA LIVRAISON"],
        end_keywords=["ADRESSE DE LA FACTURATION"],
        fallback_y_min_ratio=0.06,
        fallback_y_max_ratio=0.32,
        section_name="shipping_address",
    )

    billing_address = extract_address_section(
        blocks=blocks,
        start_keywords=["ADRESSE DE LA FACTURATION"],
        end_keywords=[
            "LIVRAISON SAUF",
            "REF FOURNISSEUR",
            "REF. FOURNISSEUR",
            "DESIGNATION",
            "CONDITIONNEMENT",
        ],
        fallback_y_min_ratio=0.18,
        fallback_y_max_ratio=0.33,
        section_name="billing_address",
    )

    return {
        "shipping_address_from_order": shipping_address,
        "billing_address_from_order": billing_address,

        # 兼容旧版 05_generate_hospital_invoice.py
        "raw_delivery_lines": shipping_address.get("display_lines", []),
        "raw_billing_lines": billing_address.get("display_lines", []),
    }


# ============================================================
# 9. 产品表提取
# ============================================================

def find_table_headers(page_blocks: List[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    找表格表头位置。
    """
    ref_header = None
    condition_header = None
    pu_header = None

    for block in page_blocks:
        simple = simplify_text(block["text"])

        if "REF" in simple and "FOURNISSEUR" in simple:
            ref_header = block

        if "CONDITIONNEMENT" in simple and "COMPTE" in simple:
            condition_header = block

        if "P.U" in simple or "PU" in simple:
            pu_header = block

    return {
        "ref_header": ref_header,
        "condition_header": condition_header,
        "pu_header": pu_header,
    }


def nearest_by_y(
    target_y: float,
    candidates: List[Dict[str, Any]],
    max_distance: float,
) -> Optional[Dict[str, Any]]:
    """
    根据 y 坐标找最近的候选。
    """
    if not candidates:
        return None

    nearest = min(candidates, key=lambda c: abs(c["y_center"] - target_y))
    distance = abs(nearest["y_center"] - target_y)

    if distance <= max_distance:
        return nearest

    return None


def extract_items_from_page(
    page_blocks: List[Dict[str, Any]],
    product_codes: List[str],
) -> Dict[str, Any]:
    """
    从单页提取产品行。

    每行提取：
        1. 产品编号
        2. Boît 数量
        3. Compte 小列数量
        4. 两个数量是否一致
    """
    if not page_blocks:
        return {"items": [], "debug": {}}

    page_number = page_blocks[0]["page"]
    page_width = page_blocks[0]["page_width"]
    page_height = page_blocks[0]["page_height"]

    headers = find_table_headers(page_blocks)

    ref_header = headers["ref_header"]
    condition_header = headers["condition_header"]
    pu_header = headers["pu_header"]

    if ref_header:
        table_y_min = ref_header["y_center"]
    else:
        table_y_min = page_height * 0.32

    y_tolerance = max(30, page_height * 0.018)

    if condition_header and pu_header:
        condition_x1 = condition_header["x1"]
        pu_x1 = pu_header["x1"]

        split_x = condition_x1 + 0.70 * (pu_x1 - condition_x1)

        boit_x_min = condition_x1 - 50
        boit_x_max = split_x

        compte_x_min = split_x
        compte_x_max = pu_x1 + 50
    else:
        boit_x_min = page_width * 0.55
        boit_x_max = page_width * 0.73

        compte_x_min = page_width * 0.73
        compte_x_max = page_width * 0.82

    product_candidates: List[Dict[str, Any]] = []

    for block in page_blocks:
        if block["y_center"] <= table_y_min:
            continue

        if block["x_center"] > page_width * 0.25:
            continue

        simple = simplify_text(block["text"])

        if "BMA" not in simple:
            continue

        validation = validate_product_code(block["text"], product_codes)

        product_candidates.append({
            **block,
            **validation,
        })

    boit_candidates: List[Dict[str, Any]] = []

    for block in page_blocks:
        if block["y_center"] <= table_y_min:
            continue

        if not (boit_x_min <= block["x_center"] <= boit_x_max):
            continue

        qty = extract_boit_quantity(block["text"])

        if qty is None:
            continue

        boit_candidates.append({
            **block,
            "quantity_from_boit": qty,
            "raw_boit_text": block["text"],
        })

    compte_candidates: List[Dict[str, Any]] = []

    for block in page_blocks:
        if block["y_center"] <= table_y_min:
            continue

        if not (compte_x_min <= block["x_center"] <= compte_x_max):
            continue

        qty = extract_compte_quantity(block["text"])

        if qty is None:
            continue

        compte_candidates.append({
            **block,
            "quantity_from_compte": qty,
            "raw_compte_text": block["text"],
        })

    items: List[Dict[str, Any]] = []

    for product in sorted(product_candidates, key=lambda b: b["y_center"]):
        boit = nearest_by_y(
            target_y=product["y_center"],
            candidates=boit_candidates,
            max_distance=y_tolerance,
        )

        compte = nearest_by_y(
            target_y=product["y_center"],
            candidates=compte_candidates,
            max_distance=y_tolerance,
        )

        quantity_from_boit = boit["quantity_from_boit"] if boit else None
        quantity_from_compte = compte["quantity_from_compte"] if compte else None

        raw_boit_text = boit["raw_boit_text"] if boit else None
        raw_compte_text = compte["raw_compte_text"] if compte else None

        quantity_status = "OK"
        quantity_warning = None
        final_quantity = None

        if quantity_from_boit is not None and quantity_from_compte is not None:
            if quantity_from_boit == quantity_from_compte:
                final_quantity = quantity_from_boit
                quantity_status = "OK"
            else:
                quantity_status = "NEEDS_REVIEW"
                quantity_warning = (
                    f"Boît 数量和 Compte 小列数量不一致："
                    f"Boît={quantity_from_boit}, Compte={quantity_from_compte}"
                )

        elif quantity_from_boit is not None and quantity_from_compte is None:
            quantity_status = "NEEDS_REVIEW"
            quantity_warning = "识别到 Boît 数量，但没有识别到 Compte 小列校验数字。"

        elif quantity_from_boit is None and quantity_from_compte is not None:
            quantity_status = "NEEDS_REVIEW"
            quantity_warning = "识别到 Compte 小列数字，但没有识别到 Boît 数量。"

        else:
            quantity_status = "NEEDS_REVIEW"
            quantity_warning = "没有识别到该产品行的数量。"

        product_status = product["product_status"]

        row_status = "OK"
        row_warnings = []

        if product_status != "OK":
            row_status = "NEEDS_REVIEW"
            row_warnings.append("产品编号不在产品数据库中，或 OCR 识别不确定。")

        if quantity_status != "OK":
            row_status = "NEEDS_REVIEW"
            row_warnings.append(quantity_warning)

        items.append({
            "page": page_number,

            "raw_product_text": product["raw_product_text"],
            "product_code": product["product_code"],
            "product_status": product_status,
            "product_suggestions": product["product_suggestions"],

            "quantity_from_boit": quantity_from_boit,
            "quantity_from_compte": quantity_from_compte,
            "final_quantity": final_quantity,
            "quantity_status": quantity_status,
            "quantity_warning": quantity_warning,

            "raw_boit_text": raw_boit_text,
            "raw_compte_text": raw_compte_text,

            "row_status": row_status,
            "row_warnings": row_warnings,

            "debug": {
                "product_y": product["y_center"],
                "boit_y": boit["y_center"] if boit else None,
                "compte_y": compte["y_center"] if compte else None,
                "product_score": product["score"],
                "boit_score": boit["score"] if boit else None,
                "compte_score": compte["score"] if compte else None,
            },
        })

    debug = {
        "page": page_number,
        "table_y_min": table_y_min,
        "y_tolerance": y_tolerance,
        "boit_x_range": [boit_x_min, boit_x_max],
        "compte_x_range": [compte_x_min, compte_x_max],
        "product_candidates_count": len(product_candidates),
        "boit_candidates_count": len(boit_candidates),
        "compte_candidates_count": len(compte_candidates),
    }

    return {
        "items": items,
        "debug": debug,
    }


def extract_all_items(
    blocks: List[Dict[str, Any]],
    product_codes: List[str],
) -> Dict[str, Any]:
    """
    从所有页面提取产品行。
    """
    pages = sorted(set(b["page"] for b in blocks))

    all_items: List[Dict[str, Any]] = []
    debug_pages: List[Dict[str, Any]] = []

    for page in pages:
        page_blocks = [b for b in blocks if b["page"] == page]

        page_result = extract_items_from_page(
            page_blocks=page_blocks,
            product_codes=product_codes,
        )

        all_items.extend(page_result["items"])
        debug_pages.append(page_result["debug"])

    return {
        "items": all_items,
        "debug_pages": debug_pages,
    }


# ============================================================
# 10. 整体校验
# ============================================================

def validate_result(result: Dict[str, Any]) -> List[str]:
    """
    对最终提取结果做整体安全检查。
    """
    warnings: List[str] = []

    header = result.get("header", {})
    hospital = result.get("hospital", {})
    addresses = result.get("addresses", {})
    items = result.get("items", [])

    if not header.get("bon_de_commande"):
        warnings.append("没有提取到 Bon de commande。")

    if not header.get("order_date"):
        warnings.append("没有提取到 Date de commande。")

    shipping = addresses.get("shipping_address_from_order", {})
    billing = addresses.get("billing_address_from_order", {})

    if shipping.get("status") != "OK":
        warnings.append(f"收货地址需要人工确认：{shipping.get('message')}")

    if billing.get("status") != "OK":
        warnings.append(f"账单地址需要人工确认：{billing.get('message')}")

    if hospital.get("match_status") == "NEEDS_REVIEW":
        warnings.append("医院数据库匹配结果需要人工确认。")

    if not items:
        warnings.append("没有提取到任何产品行。")

    for idx, item in enumerate(items, start=1):
        if item.get("row_status") != "OK":
            warnings.append(
                f"第 {idx} 个产品行需要人工确认："
                f"{item.get('raw_product_text')}"
            )

    code_counts: Dict[str, int] = {}

    for item in items:
        code = item.get("product_code")
        if code:
            code_counts[code] = code_counts.get(code, 0) + 1

    duplicated = [
        code for code, count in code_counts.items()
        if count > 1
    ]

    if duplicated:
        warnings.append(
            "发现重复产品编号，需要人工确认是否为正常重复："
            + ", ".join(duplicated)
        )

    return warnings


# ============================================================
# 11. 主提取流程
# ============================================================

def extract_order(
    ocr_dir: Path,
    product_db_path: Path,
    hospital_db_path: Optional[Path],
) -> Dict[str, Any]:
    """
    主提取函数。
    """
    print("[INFO] 读取产品数据库...")
    product_codes = load_product_codes(product_db_path)
    print(f"[INFO] 产品编号数量：{len(product_codes)}")

    print("[INFO] 读取医院数据库...")
    hospitals = load_hospital_database(hospital_db_path)
    print(f"[INFO] 医院记录数量：{len(hospitals)}")

    print("[INFO] 读取 OCR JSON blocks...")
    blocks = load_ocr_blocks(ocr_dir)
    print(f"[INFO] OCR text blocks 数量：{len(blocks)}")

    print("[INFO] 提取订单头...")
    header = extract_header(blocks)

    print("[INFO] 提取收货地址和账单地址...")
    addresses = extract_addresses(blocks)

    print("[INFO] 匹配医院数据库...")
    hospital_match = match_hospital(
        shipping_address=addresses["shipping_address_from_order"],
        billing_address=addresses["billing_address_from_order"],
        hospitals=hospitals,
    )

    print("[INFO] 提取产品行...")
    items_result = extract_all_items(
        blocks=blocks,
        product_codes=product_codes,
    )

    result: Dict[str, Any] = {
        "header": header,

        "hospital": hospital_match,

        "addresses": {
            "shipping_address_from_order": addresses["shipping_address_from_order"],
            "billing_address_from_order": addresses["billing_address_from_order"],

            # 兼容旧版发票生成逻辑
            "raw_delivery_lines": addresses["raw_delivery_lines"],
            "raw_billing_lines": addresses["raw_billing_lines"],

            # 如果提供了医院数据库，这里是数据库中匹配到的标准账单地址
            "billing_address_from_database": hospital_match.get("billing_address_from_database"),
        },

        "items": items_result["items"],

        "debug": {
            "item_extraction_pages": items_result["debug_pages"],
            "product_database_count": len(product_codes),
            "hospital_database_count": len(hospitals),
        },
    }

    result["warnings"] = validate_result(result)

    result["summary"] = {
        "bon_de_commande": header.get("bon_de_commande"),
        "order_date": header.get("order_date"),

        "hospital_match_status": hospital_match.get("match_status"),
        "hospital_match_score": hospital_match.get("match_score"),
        "matched_hospital": hospital_match.get("matched_name_in_database"),

        "shipping_status": addresses["shipping_address_from_order"].get("status"),
        "billing_status": addresses["billing_address_from_order"].get("status"),

        "item_count": len(result["items"]),
        "warning_count": len(result["warnings"]),
        "all_ok": len(result["warnings"]) == 0,
    }

    return result


# ============================================================
# 12. 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract structured order data from hospital order OCR results."
    )

    parser.add_argument(
        "--pdf",
        type=str,
        default=None,
        help="医院订单 PDF 路径。提供后会先执行 OCR。",
    )

    parser.add_argument(
        "--ocr-dir",
        type=str,
        required=True,
        help="OCR JSON 文件夹路径。比如 outputs/ocr_pages",
    )

    parser.add_argument(
        "--products",
        type=str,
        required=True,
        help="产品数据库 Excel 路径。比如 data/product_database.xlsx",
    )

    parser.add_argument(
        "--hospitals",
        type=str,
        default=None,
        help="医院数据库 Excel/CSV 路径，可选。比如 data/hospital_database.xlsx",
    )

    parser.add_argument(
        "--out",
        type=str,
        required=True,
        help="输出 JSON 路径。比如 outputs/extracted_order.json",
    )

    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="强制重新 OCR。",
    )

    args = parser.parse_args()

    ocr_dir = Path(args.ocr_dir)
    product_db_path = Path(args.products)
    hospital_db_path = Path(args.hospitals) if args.hospitals else None
    out_path = Path(args.out)

    ocr_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.pdf:
        pdf_path = Path(args.pdf)

        if not pdf_path.exists():
            raise FileNotFoundError(f"找不到 PDF 文件：{pdf_path}")

        run_paddleocr_for_pdf(
            pdf_path=pdf_path,
            ocr_dir=ocr_dir,
            lang="fr",
            force_ocr=args.force_ocr,
        )

    result = extract_order(
        ocr_dir=ocr_dir,
        product_db_path=product_db_path,
        hospital_db_path=hospital_db_path,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n==============================")
    print("[DONE] 医院订单信息提取完成")
    print("==============================")
    print(f"输出文件：{out_path}")
    print(f"Bon de commande: {result['summary']['bon_de_commande']}")
    print(f"Date de commande: {result['summary']['order_date']}")
    print(f"Matched hospital: {result['summary']['matched_hospital']}")
    print(f"Hospital match score: {result['summary']['hospital_match_score']}")
    print(f"Shipping status: {result['summary']['shipping_status']}")
    print(f"Billing status: {result['summary']['billing_status']}")
    print(f"产品行数: {result['summary']['item_count']}")
    print(f"Warnings: {result['summary']['warning_count']}")

    if result["warnings"]:
        print("\n需要人工确认的问题：")
        for warning in result["warnings"]:
            print(f"- {warning}")


if __name__ == "__main__":
    main()