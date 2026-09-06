import json
from typing import Any, Dict, Tuple

from documents.services.document_numbering_service import (
    parse_document_date,
)


def parse_business_date(value: Any):
    if value in (None, ""):
        return None

    try:
        return parse_document_date(value)
    except (TypeError, ValueError):
        return None


def get_extracted_order_date_value(
    extracted_data: Any,
) -> Tuple[Any, str]:
    data = extracted_data or {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}

    if not isinstance(data, dict):
        return None, ""

    header = data.get("header") or {}
    summary = data.get("summary") or {}

    if (
        isinstance(header, dict)
        and header.get("order_date")
    ):
        return (
            header["order_date"],
            "header.order_date",
        )

    if (
        isinstance(summary, dict)
        and summary.get("order_date")
    ):
        return (
            summary["order_date"],
            "summary.order_date",
        )

    return None, ""


def resolve_extracted_order_date(
    extracted_data: Dict[str, Any],
):
    raw_value, source = (
        get_extracted_order_date_value(
            extracted_data
        )
    )
    return parse_business_date(raw_value), source
