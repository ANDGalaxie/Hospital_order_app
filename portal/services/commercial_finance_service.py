"""Adapt frozen purchase/invoice sources to the original finance calculations.

Never query Factory PO, Settlement or current pricing. Only business-field DTOs
leave this adapter; validation models and full frozen payloads stay internal.
"""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _

from commercial_pos.services.price_service import CommercialPOError
from commercial_pos.services.snapshot_service import validated_snapshot
from documents.models import GeneratedDocument
from finance.services.finance_analysis_service import calculate_hospital_revenue
from finance.services.settlement_finance_service import (
    ZERO, add_accrual_amount, build_accrual_chart, calculate_accrual_totals,
    format_money, format_percent, money,
)
from orders.models import OrderItem
from portal.commercial_access import document_matches_batch
from portal.services.common import get_user_display_name
from portal.services.commercial_showcase_service import file_links
from portal.services.settlement_portal_service import get_frozen_document_amount


def _number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Invalid frozen amount")
    return result


def _purchase(document):
    data = validated_snapshot(document)
    amount = (get_frozen_document_amount(document, "po_data") if "po_data" in data
              else _number(data["total_amount"]))
    if amount is None or amount <= ZERO:
        raise ValueError("Invalid purchase total")
    return {"amount": money(amount), "links": file_links(document), "date": date.fromisoformat(data["shipping_date"]),
            "bon": data["bon_de_commande"], "hospital": data["hospital_name"] or "—",
            "units": _number(data["total_units"]),
            "lines": {item["product_code"]: {"quantity": _number(item["quantity"]), "amount": money(item["line_amount"])}
                      for item in data["items"]}}


def _invoice(document):
    if not document_matches_batch(document):
        raise ValueError("Invoice binding mismatch")
    amount = get_frozen_document_amount(document, "invoice_data")
    if amount is None or amount < ZERO:
        raise ValueError("Invalid invoice total")
    source = document.source_data
    payload = source["invoice_data"]
    header = payload.get("invoice") or {}
    if source.get("currency", payload.get("currency", header.get("currency", "EUR"))) != "EUR":
        raise ValueError("Unsupported invoice currency")
    invoice_date = None
    raw_date = header.get("invoice_date")
    if raw_date:
        try:
            invoice_date = parse_date(str(raw_date)) or datetime.strptime(str(raw_date), "%d/%m/%Y").date()
        except ValueError:
            pass
    lines = {}
    for item in payload.get("items") or []:
        try:
            code = item["product_code"]
            quantity = _number(item.get("quantity_raw", item.get("quantity")))
            line_amount = _number(item.get("amount_raw", item.get("line_total")))
            if not isinstance(code, str) or not code or quantity < ZERO:
                raise ValueError
            row = lines.setdefault(code, {"quantity": ZERO, "amount": ZERO})
            row["quantity"] += quantity
            row["amount"] += money(line_amount)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            # The reliable frozen document total remains usable. Unknown product
            # allocations never fall back to current prices or a made-up split.
            lines = {}
            break
    return {"amount": money(amount), "links": file_links(document), "date": invoice_date, "lines": lines}


def _records():
    documents = GeneratedDocument.objects.filter(
        document_type__in=["commercial_po", "hospital_invoice"], shipment_batch__isnull=False,
    ).select_related("shipment_batch__order__hospital").order_by("-generated_at", "-id")
    batches = {}
    for document in documents:
        batch = document.shipment_batch
        entry = batches.setdefault(batch.pk, {"batch": batch, "order": batch.order, "documents": {}})
        # Same selection as the existing order/batch comparison: newest document
        # per batch/type. An invalid newest snapshot cannot revive an older one.
        entry["documents"].setdefault(document.document_type, document)
    records, ignored = [], 0
    for batch_id, entry in batches.items():
        batch, order = entry["batch"], entry["order"]
        purchase = invoice = None
        invalid = False
        for kind, reader in (("commercial_po", _purchase), ("hospital_invoice", _invoice)):
            document = entry["documents"].get(kind)
            if document is None:
                continue
            try:
                if document.order_id != batch.order_id:
                    raise ValueError("Wrong order")
                result = reader(document)
                if kind == "commercial_po":
                    purchase = result
                else:
                    invoice = result
            except (CommercialPOError, KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
                ignored += 1
                invalid = True
        records.append({"order_id": batch.order_id, "batch_id": batch_id, "batch_number": batch.batch_number,
                        "bon": purchase["bon"] if purchase else str(order.bon_de_commande),
                        "hospital": purchase["hospital"] if purchase else (order.hospital.name if order.hospital_id else order.hospital_name or "—"),
                        "date": purchase["date"] if purchase else invoice["date"] if invoice else None,
                        "purchase": purchase, "invoice": invoice, "invalid": invalid})
    return records, ignored


def _order_amounts(order_ids):
    amounts, invalid = {}, set()
    for item in OrderItem.objects.filter(order_id__in=order_ids).values("order_id", "requested_quantity", "hospital_unit_price"):
        order_id = item["order_id"]
        try:
            price = _number(item["hospital_unit_price"])
            if price <= ZERO:
                raise ValueError
            amounts[order_id] = amounts.get(order_id, ZERO) + calculate_hospital_revenue(item["requested_quantity"], price)
        except (TypeError, ValueError, InvalidOperation):
            invalid.add(order_id)
    return {order_id: money(amounts[order_id]) if order_id in amounts and order_id not in invalid else None for order_id in order_ids}


def build_finance_context(request):
    values = {key: request.GET.get(key, "").strip()[:200] for key in ("date_from", "date_to", "hospital", "order")}
    errors, dates = [], {}
    for key in ("date_from", "date_to"):
        try:
            dates[key] = parse_date(values[key]) if values[key] else None
        except ValueError:
            dates[key] = None
        if values[key] and dates[key] is None:
            errors.append(_("日期格式无效，请使用 YYYY-MM-DD。"))
    if dates["date_from"] and dates["date_to"] and dates["date_from"] > dates["date_to"]:
        errors.append(_("开始日期不能晚于结束日期。"))
        dates = {"date_from": None, "date_to": None}
    records, ignored = _records()
    records = [r for r in records if values["hospital"].casefold() in r["hospital"].casefold()
               and values["order"].casefold() in r["bon"].casefold()
               and (not dates["date_from"] or (r["date"] and r["date"] >= dates["date_from"]))
               and (not dates["date_to"] or (r["date"] and r["date"] <= dates["date_to"]))]
    paired = [r for r in records if r["purchase"] is not None and r["invoice"] is not None]
    purchases = [r for r in records if r["purchase"] is not None]
    order_amounts = _order_amounts({r["order_id"] for r in records})
    total_sales = sum((r["invoice"]["amount"] for r in paired), ZERO)
    total_purchases = sum((r["purchase"]["amount"] for r in paired), ZERO)
    totals = calculate_accrual_totals(total_sales, total_purchases)
    monthly, hospitals = {}, {}
    metadata = {"month": defaultdict(lambda: {"units": ZERO, "count": 0}),
                "hospital": defaultdict(lambda: {"units": ZERO, "count": 0})}
    products = {}
    for r in paired:
        for group, key, meta in ((monthly, r["date"].strftime("%Y-%m"), metadata["month"]),
                                 (hospitals, r["hospital"], metadata["hospital"])):
            add_accrual_amount(group, key, sales=r["invoice"]["amount"], purchases=r["purchase"]["amount"])
            meta[key]["units"] += r["purchase"]["units"]
            meta[key]["count"] += 1
        for code, line in r["purchase"]["lines"].items():
            row = products.setdefault(code, {"sales": ZERO, "purchases": ZERO, "units": ZERO, "count": 0, "complete": True})
            billed = r["invoice"]["lines"].get(code)
            if billed is None or billed["quantity"] != line["quantity"]:
                row["complete"] = False
            else:
                row["sales"] += billed["amount"]
            row["purchases"] += line["amount"]
            row["units"] += line["quantity"]
            row["count"] += 1
    def financial_fields(sales, purchase):
        data = calculate_accrual_totals(sales, purchase)
        return {"sales": format_money(data["sales_total"]), "purchases": format_money(data["purchase_total"]),
                "gross_profit": format_money(data["gross_profit"]), "gross_margin": f"{format_percent(data['gross_margin'])}%"}
    def group_rows(group, meta):
        return [{"label": key, **financial_fields(row["sales"], row["purchases"]),
                 "units": str(meta[key]["units"].quantize(Decimal("1"))), "count": meta[key]["count"]} for key, row in sorted(group.items(), reverse=True)]
    monthly_rows, hospital_rows = group_rows(monthly, metadata["month"]), group_rows(hospitals, metadata["hospital"])
    product_rows = []
    for code, row in sorted(products.items()):
        fields = financial_fields(row["sales"], row["purchases"]) if row["complete"] else {
            "sales": "—", "purchases": format_money(row["purchases"]), "gross_profit": "—", "gross_margin": "—"}
        product_rows.append({"label": code, "units": str(row["units"].quantize(Decimal("1"))), "count": row["count"], **fields})
    ordered = sorted(records, key=lambda r: (r["bon"], r["batch_number"], r["batch_id"]))
    seen_orders, order_rows = set(), []
    for r in ordered:
        first = r["order_id"] not in seen_orders
        seen_orders.add(r["order_id"])
        purchase, invoice = r["purchase"], r["invoice"]
        fields = financial_fields(invoice["amount"], purchase["amount"]) if invoice and purchase else {
            "sales": format_money(invoice["amount"]) if invoice else "—", "purchases": format_money(purchase["amount"]) if purchase else "—",
            "gross_profit": "—", "gross_margin": "—"}
        order_rows.append({"label": r["bon"], "batch_number": r["batch_number"], "shipping_date": r["date"], "hospital_name": r["hospital"],
                           "order_amount": format_money(order_amounts[r["order_id"]]) if first and order_amounts[r["order_id"]] is not None else "—",
                           "detail_url": reverse("portal:commercial_operations_detail", args=[r["batch_id"]]),
                           "invoice_url": invoice["links"]["pdf"] if invoice else None,
                           "purchase_url": purchase["links"]["pdf"] if purchase else None,
                           "status": _("数据异常") if r["invalid"] else _("已生成") if invoice and purchase else _("销售发票未生成") if not invoice else _("采购订单未生成"),
                           **fields})
    kpi_help = {
        "sales": _("已匹配批次的医院发票冻结总额。"),
        "purchases": _("已匹配批次的采购订单冻结总额。"),
        "gross_profit": _("医院已开票金额减采购订单金额。"),
        "gross_margin": _("预计毛利占医院已开票金额的比例。"),
        "receipts": _("医院收款数据暂未提供。"),
        "payments": _("采购付款数据暂未提供。"),
        "cash_net": _("需收款及付款数据，暂不计算。"),
        "receivable_remaining": _("需医院收款数据，暂不计算。"),
        "payable_remaining": _("需采购付款数据，暂不计算。"),
        "overdue": _("需账期及收款数据，暂不计算。"),
    }
    cards = [{"label": _("已生成采购订单"), "value": len(purchases)},
             {"label": _("医院订单数"), "value": len({r["order_id"] for r in purchases})},
             {"label": _("产品总件数"), "value": str(sum((r["purchase"]["units"] for r in purchases), ZERO).quantize(Decimal("1")))},
             {"label": _("采购订单总金额"), "value": format_money(sum((r["purchase"]["amount"] for r in purchases), ZERO))}]
    kpis = [{"key": key, "label": label, "value": value if paired else "—", "help": kpi_help[key]}
            for key, label, value in (("sales", _("医院已开票金额"), format_money(totals["sales_total"])),
                                      ("purchases", _("采购订单金额"), format_money(totals["purchase_total"])),
                                      ("gross_profit", _("预计毛利"), format_money(totals["gross_profit"])),
                                      ("gross_margin", _("预计毛利率"), f"{format_percent(totals['gross_margin'])}%"))]
    kpis.extend({"key": key, "label": label, "value": "—", "help": kpi_help[key]}
                for key, label in (("receipts", _("医院已收金额")), ("payments", _("采购已付金额")), ("cash_net", _("实际现金净流入")),
                                   ("receivable_remaining", _("医院待收余额")), ("payable_remaining", _("采购待付余额")), ("overdue", _("逾期应收金额"))))
    original_total = sum(order_amounts.values(), ZERO) if all(value is not None for value in order_amounts.values()) else None
    return {"page_title": _("数据与财务"), "reporting_currency": "EUR", "cards": cards, "kpi_cards": kpis,
            "summary": {**{key: str(value) if paired else None for key, value in totals.items()},
                        "matched_batch_count": len(paired), "unmatched_batch_count": len(records) - len(paired)},
            "original_order_total": format_money(original_total) if original_total is not None else "—",
            "filter_values": values, "filter_errors": errors, "has_active_filters": any(values.values()), "ignored_count": ignored,
            "monthly_rows": monthly_rows, "hospital_rows": hospital_rows, "product_rows": product_rows, "order_rows": order_rows,
            "groups": [{"title": _("按发货月份汇总"), "rows": monthly_rows}, {"title": _("按医院汇总"), "rows": hospital_rows}, {"title": _("按产品汇总"), "rows": product_rows}],
            "chart_data": {"accrual": build_accrual_chart(monthly), "cash": {"labels": [], "receipts": [], "payments": [], "net_inflow": []},
                           "ui": {"empty": _("暂无趋势数据"), "sales": _("销售额"), "purchases": _("采购额"), "gross_profit": _("预计毛利"),
                                  "receipts": _("医院收款"), "payments": _("采购付款"), "net_inflow": _("现金净流入")}},
            "user_display_name": get_user_display_name(request.user)}
