import re
import unicodedata
from typing import Any, Dict, List, Tuple

from factories.models import Factory


def normalize_text(value: Any) -> str:
    """
    去重音 + 大写 + 合并空格。
    用于工厂名称和地址匹配。
    """
    if value is None:
        return ""

    text = str(value)

    text = unicodedata.normalize("NFD", text)
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch) != "Mn"
    )

    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def flatten_strings(value: Any) -> List[str]:
    """
    从 factory_data 这种复杂 JSON 中抽取所有字符串。
    """
    strings = []

    if value is None:
        return strings

    if isinstance(value, str):
        if value.strip():
            strings.append(value.strip())

    elif isinstance(value, dict):
        for v in value.values():
            strings.extend(flatten_strings(v))

    elif isinstance(value, list):
        for item in value:
            strings.extend(flatten_strings(item))

    else:
        text = str(value).strip()
        if text:
            strings.append(text)

    return strings


def split_lines(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(split_lines(item))
        return result

    text = str(value).replace("\r", "\n")
    return [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]


def score_factory_against_text(factory: Factory, full_text: str) -> Tuple[int, List[str]]:
    """
    给一个 Factory 打匹配分数。

    分数来源：
        - 工厂完整名 / 简称 / legal name
        - match_keywords
        - 地址行
    """
    score = 0
    reasons = []

    normalized_full_text = normalize_text(full_text)

    candidates = [
        ("name", factory.name, 90),
        ("legal_name", factory.legal_name, 100),
        ("short_name", factory.short_name, 80),
    ]

    for label, value, points in candidates:
        normalized_value = normalize_text(value)

        if normalized_value and normalized_value in normalized_full_text:
            score += points
            reasons.append(f"Matched {label}: {value}")

    # 关键词匹配
    for keyword in split_lines(factory.match_keywords):
        normalized_keyword = normalize_text(keyword)

        if not normalized_keyword:
            continue

        if normalized_keyword in normalized_full_text:
            score += 30
            reasons.append(f"Matched keyword: {keyword}")

    # 地址匹配
    address_lines = split_lines(factory.address)

    for line in address_lines:
        normalized_line = normalize_text(line)

        if not normalized_line:
            continue

        if normalized_line in normalized_full_text:
            score += 25
            reasons.append(f"Matched address line: {line}")
            continue

        # 如果整行没完全匹配，再看地址中的关键 token
        tokens = [
            token
            for token in normalized_line.split()
            if len(token) >= 4
        ]

        if not tokens:
            continue

        matched_tokens = [
            token
            for token in tokens
            if token in normalized_full_text
        ]

        if len(matched_tokens) >= 2:
            score += 10
            reasons.append(
                f"Partially matched address line: {line} "
                f"tokens={matched_tokens}"
            )

    return score, reasons


def match_factory_from_confirmation_data(factory_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据工厂确认文件提取出的 JSON，匹配 Factory 表。

    返回：
        factory
        status
        score
        message
    """
    strings = flatten_strings(factory_data)
    full_text = "\n".join(strings)

    factories = list(
        Factory.objects.filter(is_active=True).order_by("name")
    )

    if not factories:
        return {
            "factory": None,
            "status": "needs_review",
            "score": 0,
            "message": "No active factory found in Factory database.",
        }

    scored = []

    for factory in factories:
        score, reasons = score_factory_against_text(
            factory=factory,
            full_text=full_text,
        )

        scored.append(
            {
                "factory": factory,
                "score": score,
                "reasons": reasons,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)

    best = scored[0]

    if best["score"] >= 80:
        return {
            "factory": best["factory"],
            "status": "ok",
            "score": best["score"],
            "message": "\n".join(best["reasons"]) or "Factory matched automatically.",
        }

    suggestions = [
        f"{item['factory'].short_name or item['factory'].name}: score={item['score']}"
        for item in scored[:5]
    ]

    return {
        "factory": None,
        "status": "needs_review",
        "score": best["score"],
        "message": (
            "Factory could not be matched automatically. "
            "Please select factory manually.\n"
            + "\n".join(suggestions)
        ),
    }

def match_factory_from_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    通用工厂匹配函数。

    可以用于：
        - 医院订单 OCR blocks
        - 医院订单 extracted_order_data
        - 其他包含工厂名称 / 地址的结构化数据

    当前正式流程应该优先用于医院订单 OCR 数据。
    """
    strings = flatten_strings(data)
    full_text = "\n".join(strings)

    factories = list(
        Factory.objects.filter(is_active=True).order_by("name")
    )

    if not factories:
        return {
            "factory": None,
            "status": "needs_review",
            "score": 0,
            "message": "No active factory found in Factory database.",
        }

    scored = []

    for factory in factories:
        score, reasons = score_factory_against_text(
            factory=factory,
            full_text=full_text,
        )

        scored.append(
            {
                "factory": factory,
                "score": score,
                "reasons": reasons,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)

    best = scored[0]

    if best["score"] >= 80:
        return {
            "factory": best["factory"],
            "status": "ok",
            "score": best["score"],
            "message": "\n".join(best["reasons"]) or "Factory matched automatically.",
        }

    suggestions = [
        f"{item['factory'].short_name or item['factory'].name}: score={item['score']}"
        for item in scored[:5]
    ]

    return {
        "factory": None,
        "status": "needs_review",
        "score": best["score"],
        "message": (
            "Factory could not be matched automatically from hospital order. "
            "Please select factory manually.\n"
            + "\n".join(suggestions)
        ),
    }