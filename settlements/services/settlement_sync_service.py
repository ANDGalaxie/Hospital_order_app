import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from documents.models import GeneratedDocument
from settlements.models import SettlementAccount


MONEY_QUANT = Decimal("0.01")

SUPPORTED_DOCUMENT_TYPES = {
    GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
    GeneratedDocument.DocumentType.FACTORY_PO,
}


class SettlementSyncError(ValueError):
    """
    文档无法转换为结算账户时抛出的业务异常。
    """


def parse_money(value):
    """
    将数值或格式化金额转换为 Decimal。

    支持：
    - 500
    - 500.00
    - "€500.00"
    - "1,234.50"
    - "1 234,50 €"
    """
    if value is None or value == "":
        return None

    if isinstance(value, Decimal):
        return value.quantize(MONEY_QUANT)

    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(
            MONEY_QUANT
        )

    text = str(value).strip()
    text = text.replace("\u00a0", "")
    text = text.replace(" ", "")
    text = re.sub(
        r"[^0-9,.\-]",
        "",
        text,
    )

    if not text:
        return None

    comma_position = text.rfind(",")
    dot_position = text.rfind(".")

    if comma_position >= 0 and dot_position >= 0:
        if comma_position > dot_position:
            # 1.234,50
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            # 1,234.50
            text = text.replace(",", "")

    elif comma_position >= 0:
        decimal_digits = (
            len(text)
            - comma_position
            - 1
        )

        if decimal_digits in {1, 2}:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        return Decimal(text).quantize(
            MONEY_QUANT
        )
    except InvalidOperation:
        return None


def parse_document_date(value):
    """
    兼容当前和历史文档中的常见日期格式。
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d.%m.%Y",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            continue

    return None


def get_document_payload(document):
    """
    返回真正生成 Invoice 或 PO 时使用的数据。
    """
    source_data = document.source_data or {}

    if not isinstance(source_data, dict):
        return {}

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        payload = source_data.get(
            "invoice_data"
        )

        if isinstance(payload, dict):
            return payload

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_PO
    ):
        payload = source_data.get(
            "po_data"
        )

        if isinstance(payload, dict):
            return payload

    # 兼容旧文档将数据直接存放在顶层。
    return source_data


def extract_document_amount(document):
    """
    读取正式文档金额。

    优先读取 totals 中的原始数值，
    然后兼容格式化金额和产品行求和。
    """
    payload = get_document_payload(
        document
    )

    totals = payload.get("totals") or {}

    if not isinstance(totals, dict):
        totals = {}

    amount_keys = [
        "total_raw",
        "total_amount_raw",
        "actual_factory_total",
        "grand_total_raw",
        "untaxed_amount_raw",
        "total",
        "total_amount",
        "amount",
    ]

    for key in amount_keys:
        if key not in totals:
            continue

        amount = parse_money(
            totals.get(key)
        )

        if amount is not None:
            return amount

    for key in amount_keys:
        if key not in payload:
            continue

        amount = parse_money(
            payload.get(key)
        )

        if amount is not None:
            return amount

    items = payload.get("items") or []

    if not isinstance(items, list):
        return None

    total = Decimal("0.00")
    found_amount = False

    for item in items:
        if not isinstance(item, dict):
            continue

        amount = parse_money(
            item.get("amount_raw")
            or item.get("amount")
        )

        if amount is not None:
            total += amount
            found_amount = True

    if found_amount:
        return total.quantize(
            MONEY_QUANT
        )

    return None


def get_document_generated_date(document):
    generated_at = document.generated_at

    if not generated_at:
        return timezone.localdate()

    if timezone.is_aware(generated_at):
        return timezone.localtime(
            generated_at
        ).date()

    return generated_at.date()


def extract_issue_date(document):
    payload = get_document_payload(
        document
    )

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        invoice = payload.get("invoice") or {}

        if isinstance(invoice, dict):
            value = (
                invoice.get("invoice_date_iso")
                or invoice.get("invoice_date")
                or invoice.get("issue_date")
            )

            parsed = parse_document_date(
                value
            )

            if parsed:
                return parsed

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_PO
    ):
        po = payload.get("po") or {}

        if isinstance(po, dict):
            value = (
                po.get("order_date_iso")
                or po.get("order_date")
                or po.get("po_date")
            )

            parsed = parse_document_date(
                value
            )

            if parsed:
                return parsed

    for key in [
        "issue_date",
        "invoice_date",
        "order_date",
        "document_date",
    ]:
        parsed = parse_document_date(
            payload.get(key)
        )

        if parsed:
            return parsed

    return get_document_generated_date(
        document
    )


def extract_due_date(document):
    if (
        document.document_type
        != GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        # 当前 Factory PO 没有可靠的付款截止日期。
        return None

    payload = get_document_payload(
        document
    )

    invoice = payload.get("invoice") or {}

    if isinstance(invoice, dict):
        value = (
            invoice.get("due_date_iso")
            or invoice.get("due_date")
            or invoice.get(
                "payment_due_date"
            )
        )

        parsed = parse_document_date(
            value
        )

        if parsed:
            return parsed

    for key in [
        "due_date",
        "payment_due_date",
    ]:
        parsed = parse_document_date(
            payload.get(key)
        )

        if parsed:
            return parsed

    return None


def extract_currency(document):
    payload = get_document_payload(
        document
    )

    nested_data = {}

    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        nested_data = (
            payload.get("invoice")
            or {}
        )

    elif (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .FACTORY_PO
    ):
        nested_data = (
            payload.get("po")
            or {}
        )

    candidates = [
        nested_data.get("currency")
        if isinstance(nested_data, dict)
        else None,
        payload.get("currency"),
    ]

    for candidate in candidates:
        value = str(
            candidate or ""
        ).strip().upper()

        if len(value) == 3:
            return value

    return "EUR"


def extract_invoice_counterparty(document):
    order = document.order

    hospital = getattr(
        order,
        "hospital",
        None,
    )

    if hospital:
        hospital_name = str(
            getattr(hospital, "name", "")
            or ""
        ).strip()

        if hospital_name:
            return hospital_name

    hospital_name = str(
        getattr(
            order,
            "hospital_name",
            "",
        )
        or ""
    ).strip()

    return (
        hospital_name
        or "Unknown Hospital"
    )


def extract_po_counterparty(document):
    payload = get_document_payload(
        document
    )

    factory_payload = (
        payload.get("factory")
        or {}
    )

    if isinstance(factory_payload, dict):
        for key in [
            "name",
            "factory_name",
            "company_name",
            "short_name",
        ]:
            value = str(
                factory_payload.get(key)
                or ""
            ).strip()

            if value:
                return value

    order = document.order

    factory = getattr(
        order,
        "factory",
        None,
    )

    if factory:
        full_name = str(
            getattr(factory, "name", "")
            or ""
        ).strip()

        if full_name:
            return full_name

        short_name = str(
            getattr(
                factory,
                "short_name",
                "",
            )
            or ""
        ).strip()

        if short_name:
            return short_name

    return "Unknown Factory"


def extract_counterparty_name(document):
    if (
        document.document_type
        == GeneratedDocument
        .DocumentType
        .HOSPITAL_INVOICE
    ):
        return extract_invoice_counterparty(
            document
        )

    return extract_po_counterparty(
        document
    )


def build_settlement_account_data(document):
    """
    将 GeneratedDocument 转换为创建结算账户所需的数据。
    """
    direction = (
        SettlementAccount
        .expected_direction_for_document(
            document
        )
    )

    if direction is None:
        raise SettlementSyncError(
            "该文档类型不支持创建结算账户。"
        )

    amount = extract_document_amount(
        document
    )

    if amount is None:
        raise SettlementSyncError(
            "无法从文档快照中读取结算金额。"
        )

    if amount <= 0:
        raise SettlementSyncError(
            "文档结算金额必须大于 0。"
        )

    issue_date = extract_issue_date(
        document
    )

    due_date = extract_due_date(
        document
    )

    if (
        due_date
        and due_date < issue_date
    ):
        raise SettlementSyncError(
            "付款截止日期早于文档开立日期。"
        )

    return {
        "direction": direction,
        "counterparty_name": (
            extract_counterparty_name(
                document
            )
        ),
        "issue_date": issue_date,
        "due_date": due_date,
        "currency": extract_currency(
            document
        ),
        "original_amount": amount,
    }


@transaction.atomic
def sync_document_to_settlement(
    document,
    *,
    dry_run=False,
):
    """
    同步一份正式文档。

    返回状态：
    - created
    - would_create
    - existing
    - unsupported
    - error
    """
    document_query = (
        GeneratedDocument.objects
    )

    # Dry run 只读取，不需要数据库行锁。
    # 正式同步只锁定 GeneratedDocument 本身。
    # 不在 select_for_update 查询中连接可空关系，
    # 否则 PostgreSQL 会报 nullable outer join 错误。
    if not dry_run:
        document_query = (
            document_query
            .select_for_update()
        )

    document = document_query.get(
        pk=document.pk
    )

    if (
        document.document_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        return {
            "status": "unsupported",
            "document_id": document.id,
            "document_number": (
                document.document_number
            ),
            "message": (
                "Factory Order Request "
                "不创建结算账户。"
            ),
            "account": None,
        }

    existing_account = (
        SettlementAccount.objects
        .filter(document=document)
        .first()
    )

    if existing_account:
        return {
            "status": "existing",
            "document_id": document.id,
            "document_number": (
                document.document_number
            ),
            "message": (
                "结算账户已经存在。"
            ),
            "account": existing_account,
        }

    try:
        account_data = (
            build_settlement_account_data(
                document
            )
        )

        account = SettlementAccount(
            document=document,
            **account_data,
        )

        account.full_clean()

        if dry_run:
            return {
                "status": "would_create",
                "document_id": document.id,
                "document_number": (
                    document.document_number
                ),
                "message": (
                    "校验通过，可以创建。"
                ),
                "account": account,
            }

        account.save()

        return {
            "status": "created",
            "document_id": document.id,
            "document_number": (
                document.document_number
            ),
            "message": (
                "结算账户创建成功。"
            ),
            "account": account,
        }

    except (
        SettlementSyncError,
        ValidationError,
        ValueError,
    ) as exc:
        return {
            "status": "error",
            "document_id": document.id,
            "document_number": (
                document.document_number
            ),
            "message": str(exc),
            "account": None,
        }


def sync_all_settlement_accounts(
    *,
    dry_run=False,
    limit=None,
):
    """
    扫描所有正式 Invoice 和 Factory PO。

    该操作具备幂等性：
    重复执行不会创建重复账户。
    """
    documents = (
        GeneratedDocument.objects
        .filter(
            document_type__in=(
                SUPPORTED_DOCUMENT_TYPES
            )
        )
        .select_related(
            "order",
            "order__hospital",
            "order__factory",
            "shipment_batch",
        )
        .order_by(
            "generated_at",
            "id",
        )
    )

    if limit:
        documents = documents[:limit]

    report = {
        "scanned": 0,
        "created": 0,
        "would_create": 0,
        "existing": 0,
        "unsupported": 0,
        "error": 0,
        "results": [],
    }

    for document in documents:
        result = (
            sync_document_to_settlement(
                document,
                dry_run=dry_run,
            )
        )

        report["scanned"] += 1
        report[result["status"]] += 1
        report["results"].append(
            result
        )

    return report
