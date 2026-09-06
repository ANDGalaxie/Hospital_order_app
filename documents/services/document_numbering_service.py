from datetime import date, datetime
from typing import Any, Dict, Optional

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from documents.models import DocumentSequence


def normalize_bon_de_commande(value: Any) -> str:
    """
    标准化订单号。

    例如：
        "150222" -> "150222"
        "BON DE COMMANDE N° 150222" -> "150222"
    """
    if value is None:
        return ""

    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())

    return digits or text


def parse_document_date(document_date: Optional[Any] = None) -> date:
    """
    把 document_date 转成 date 对象。

    允许：
        None
        date
        datetime
        "2026-06-09"
        "09/06/2026"
    """
    if document_date is None:
        return timezone.localdate()

    if isinstance(document_date, datetime):
        return document_date.date()

    if isinstance(document_date, date):
        return document_date

    text = str(document_date).strip()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    raise ValueError(
        f"Invalid document_date={document_date}. "
        "Expected YYYY-MM-DD or DD/MM/YYYY."
    )


def get_month_key(document_date: date) -> str:
    """
    生成月份 key。

    例如：
        2026-06-09 -> "2026-06"
    """
    return document_date.strftime("%Y-%m")


def build_invoice_number(document_date: date, sequence: int) -> str:
    """
    生成发票编号。

    规则：
        Invoice + 年 + 两位流水号 + 两位月份

    例如：
        document_date = 2026-06-09
        sequence = 1
        -> Invoice 20260106
    """
    year = document_date.strftime("%Y")
    month = document_date.strftime("%m")
    return f"Invoice {year}{sequence:02d}{month}"


def build_po_number(document_date: date, sequence: int) -> str:
    """
    生成工厂采购订单编号。

    规则：
        DELAHK + 两位流水号 + 两位月份 + S

    例如：
        document_date = 2026-06-09
        sequence = 1
        -> DELAHK0106S
    """
    month = document_date.strftime("%m")
    return f"DELAHK{sequence:02d}{month}S"


def get_next_sequence_for_month(month_key: str) -> int:
    """
    查询当前月份的下一个 sequence。

    例如：
        当前 2026-06 最大 sequence = 3
        下一个就是 4
    """
    max_sequence = (
        DocumentSequence.objects
        .filter(month_key=month_key)
        .aggregate(max_sequence=Max("sequence"))
        .get("max_sequence")
    )

    if max_sequence is None:
        return 1

    return int(max_sequence) + 1


@transaction.atomic
def get_or_create_document_numbers(
    bon_de_commande: Any,
    document_date: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    为一个订单获取或创建 Invoice / PO 编号。

    重要规则：
        1. 同一个 bon_de_commande + 同一个月份，只能有一个 sequence。
        2. 同一个订单重复生成文件时，复用旧编号。
        3. Invoice 和 PO 共用同一个 sequence。
    """
    bon = normalize_bon_de_commande(bon_de_commande)

    if not bon:
        raise ValueError("bon_de_commande is empty.")

    doc_date = parse_document_date(document_date)
    month_key = get_month_key(doc_date)

    existing = (
        DocumentSequence.objects
        .select_for_update()
        .filter(
            month_key=month_key,
            bon_de_commande=bon,
        )
        .first()
    )

    if existing:
        if not existing.po_number:
            existing.po_number = build_po_number(
                document_date=doc_date,
                sequence=existing.sequence,
            )
            existing.save(
                update_fields=[
                    "po_number",
                    "updated_at",
                ]
            )

        return {
            "created": False,
            "sequence_id": existing.id,
            "month_key": existing.month_key,
            "bon_de_commande": existing.bon_de_commande,
            "sequence": existing.sequence,
            "invoice_number": existing.invoice_number,
            "po_number": existing.po_number,
            "document_date": doc_date.isoformat(),
        }

    sequence = get_next_sequence_for_month(month_key)

    invoice_number = build_invoice_number(
        document_date=doc_date,
        sequence=sequence,
    )

    po_number = build_po_number(
        document_date=doc_date,
        sequence=sequence,
    )

    try:
        obj = DocumentSequence.objects.create(
            month_key=month_key,
            bon_de_commande=bon,
            sequence=sequence,
            invoice_number=invoice_number,
            po_number=po_number,
        )

    except IntegrityError:
        # 极少数情况下，如果同时生成两个订单，可能撞到唯一约束。
        # 简单重试一次。
        sequence = get_next_sequence_for_month(month_key)

        invoice_number = build_invoice_number(
            document_date=doc_date,
            sequence=sequence,
        )

        po_number = build_po_number(
            document_date=doc_date,
            sequence=sequence,
        )

        obj = DocumentSequence.objects.create(
            month_key=month_key,
            bon_de_commande=bon,
            sequence=sequence,
            invoice_number=invoice_number,
            po_number=po_number,
        )

    return {
        "created": True,
        "sequence_id": obj.id,
        "month_key": obj.month_key,
        "bon_de_commande": obj.bon_de_commande,
        "sequence": obj.sequence,
        "invoice_number": obj.invoice_number,
        "po_number": obj.po_number,
        "document_date": doc_date.isoformat(),
    }


# Invoice audit / future-numbering source of truth.  These helpers have no writes.
def invoice_order_sort_key(order):
    if not order.order_date:
        raise ValueError(f"Order {order.id} ({order.bon_de_commande}) is missing order_date; Invoice numbering is blocked.")
    digits = "".join(ch for ch in str(order.bon_de_commande) if ch.isdigit())
    return (order.order_date, int(digits) if digits else float("inf"), str(order.bon_de_commande), order.id)


def compute_expected_invoice_sequence(order, orders=None):
    from orders.models import Order
    if not order.order_date:
        raise ValueError(f"Order {order.id} ({order.bon_de_commande}) is missing order_date; Invoice numbering is blocked.")
    candidates = list(orders) if orders is not None else list(Order.objects.filter(order_date__year=order.order_date.year, order_date__month=order.order_date.month))
    candidates = [candidate for candidate in candidates if candidate.order_date]
    candidates.sort(key=invoice_order_sort_key)
    return candidates.index(next(candidate for candidate in candidates if candidate.id == order.id)) + 1


def build_expected_invoice_number(order, orders=None):
    sequence = compute_expected_invoice_sequence(order, orders)
    return {"year": order.order_date.year, "month": order.order_date.month, "sequence": sequence, "sort_key": invoice_order_sort_key(order), "invoice_number": f"Invoice {order.order_date:%Y}{sequence:02d}{order.order_date:%m}"}


@transaction.atomic
def get_or_create_expected_invoice_numbers(order):
    from documents.models import DocumentSequence
    expected=build_expected_invoice_number(order)
    month_key=order.order_date.strftime("%Y-%m")
    month_orders=list(__import__("orders.models", fromlist=["Order"]).Order.objects.filter(order_date__year=order.order_date.year, order_date__month=order.order_date.month))
    for other in month_orders:
        existing=DocumentSequence.objects.filter(month_key=month_key, bon_de_commande=other.bon_de_commande).first()
        if existing:
            other_expected=build_expected_invoice_number(other, month_orders)
            if existing.sequence != other_expected["sequence"] or existing.invoice_number != other_expected["invoice_number"]:
                raise ValueError(f"Invoice numbering requires historical rebuild: Order {other.id} ({other.bon_de_commande}) current={existing.invoice_number} expected={other_expected['invoice_number']}.")
    existing=DocumentSequence.objects.select_for_update().filter(month_key=month_key, bon_de_commande=order.bon_de_commande).first()
    if existing:
        return {**expected, "sequence_id": existing.id, "created": False}
    obj = DocumentSequence.objects.create(
        month_key=month_key,
        bon_de_commande=order.bon_de_commande,
        sequence=expected["sequence"],
        invoice_number=expected["invoice_number"],
        po_number=build_po_number(
            document_date=order.order_date,
            sequence=expected["sequence"],
        ),
    )
    return {**expected, "sequence_id": obj.id, "created": True}
