"""Read-only company-wide EUR analysis, with separate accrual and payment dates."""
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from administrative_expenses.categories import CATEGORIES, SUBCATEGORIES
from administrative_expenses.models import AdministrativeExpense
from administrative_expenses.services import month_bounds
from documents.models import GeneratedDocument
from settlements.models import PaymentTransaction, SettlementAccount

from .settlement_finance_service import (
    ZERO, build_filtered_account_queryset, calculate_accrual_totals,
    format_money, format_percent, money,
)


def _shift_month(start, offset):
    year, index = divmod(start.year * 12 + start.month - 1 + offset, 12)
    return date(year, index + 1, 1)


def _month_key(start):
    return f"{start.year:04d}-{start.month:02d}"


def operating_period(month=None):
    # An absent parameter selects this month; an explicitly empty parameter is invalid.
    if month == "":
        raise ValidationError(_("Choose a valid month (YYYY-MM)."))
    start, end = month_bounds(month)
    try:
        first = _shift_month(start, -11)
    except ValueError as exc:
        raise ValidationError(_("Choose a month that supports a complete 12-month reporting period.")) from exc
    return start, end, first, [_shift_month(first, index) for index in range(12)]


def _valid_accounts():
    # Reuse EUR/cancellation rules without passing an issue-date filter.
    return build_filtered_account_queryset(reporting_currency="EUR").filter(
        Q(direction=SettlementAccount.Direction.RECEIVABLE,
          document__document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE)
        | Q(direction=SettlementAccount.Direction.PAYABLE,
            document__document_type=GeneratedDocument.DocumentType.FACTORY_PO)
    )


def _grouped_amounts(queryset, date_field, amount_field, *dimensions):
    return (
        queryset.order_by().annotate(month=TruncMonth(date_field))
        .values("month", *dimensions).annotate(total=Sum(amount_field))
    )


def _percent_display(ratio):
    return "—" if ratio is None else f"{format_percent(ratio):.2f}%"


def _summary(row):
    accrual = calculate_accrual_totals(row["invoiced_sales"], row["factory_purchases"])
    operating_balance = money(accrual["gross_profit"] - row["administrative_expenses_total"])
    sales = accrual["sales_total"]
    return {
        **row, "gross_profit": accrual["gross_profit"],
        "gross_margin": accrual["gross_margin"] if sales > ZERO else None,
        "estimated_operating_balance": operating_balance,
        "estimated_operating_margin": operating_balance / sales if sales > ZERO else None,
        "estimated_net_cash_inflow": money(
            row["hospital_receipts"] - row["factory_payments"] - row["paid_administrative_expenses"]
        ),
    }


def _category_rows(amounts, selected_month, total):
    rows = []
    for code, label in CATEGORIES.items():
        children = []
        for subcode, sublabel in SUBCATEGORIES.get(code, {}).items():
            amount = amounts.get((code, subcode), ZERO)
            children.append({
                "label": sublabel, "code": subcode, "amount": amount,
                "amount_display": format_money(amount),
                "share": amount / total if total > ZERO else None,
                "share_display": _percent_display(amount / total if total > ZERO else None),
                "url": reverse("portal:administrative_expenses:list", args=[code, subcode]) + "?month=" + selected_month,
            })
        amount = money(sum((value for (category, _sub), value in amounts.items() if category == code), ZERO))
        rows.append({
            "label": label, "code": code, "amount": amount,
            "amount_display": format_money(amount),
            "share": amount / total if total > ZERO else None,
            "share_display": _percent_display(amount / total if total > ZERO else None),
            "url": reverse("portal:administrative_expenses:category", args=[code]) + "?month=" + selected_month,
            "children": children,
        })
    return rows


def _kpi_groups(summary):
    specs = [
        ("accrual", _("Sales and factory purchases"), [
            ("invoiced_sales", _("Hospital invoiced sales")),
            ("factory_purchases", _("Factory purchases")),
            ("gross_profit", _("Estimated gross profit")),
            ("gross_margin", _("Estimated gross margin")),
        ]),
        ("operations", _("Company operating estimate"), [
            ("administrative_expenses_total", _("Company administrative expenses")),
            ("estimated_operating_balance", _("Estimated operating balance")),
            ("estimated_operating_margin", _("Estimated operating margin")),
        ]),
        ("cash", _("Registered cash flow"), [
            ("hospital_receipts", _("Registered hospital receipts")),
            ("factory_payments", _("Registered factory payments")),
            ("paid_administrative_expenses", _("Paid administrative expenses")),
            ("estimated_net_cash_inflow", _("Estimated net cash inflow")),
        ]),
    ]
    return [
        {"key": key, "label": label, "cards": [
            {"key": field, "label": title, "value": (
                _percent_display(summary[field]) if field.endswith("margin") else format_money(summary[field])
            )}
            for field, title in cards
        ]}
        for key, label, cards in specs
    ]


def build_operating_finance_context(*, month=None):
    """Four bounded aggregate queries; never select payroll names or attachments."""
    start, end, first, months = operating_period(month)
    selected_month = _month_key(start)
    monthly = {
        value: {field: ZERO for field in (
            "invoiced_sales", "factory_purchases", "administrative_expenses_total",
            "hospital_receipts", "factory_payments", "paid_administrative_expenses",
        )}
        for value in months
    }
    accounts = _valid_accounts()
    for row in _grouped_amounts(
        accounts.filter(issue_date__gte=first, issue_date__lt=end),
        "issue_date", "original_amount", "direction",
    ):
        field = "invoiced_sales" if row["direction"] == SettlementAccount.Direction.RECEIVABLE else "factory_purchases"
        monthly[row["month"]][field] = money(row["total"])

    # Payment-date filtering is independent of the document's issue month.
    transactions = PaymentTransaction.objects.filter(
        status=PaymentTransaction.Status.POSTED,
        account_id__in=accounts.order_by().values("pk"),
        payment_date__gte=first, payment_date__lt=end,
    )
    for row in _grouped_amounts(transactions, "payment_date", "amount", "account__direction"):
        field = "hospital_receipts" if row["account__direction"] == SettlementAccount.Direction.RECEIVABLE else "factory_payments"
        monthly[row["month"]][field] = money(row["total"])

    expenses = AdministrativeExpense.objects.filter(is_void=False)
    category_amounts = {}
    for row in _grouped_amounts(
        expenses.filter(expense_date__gte=first, expense_date__lt=end),
        "expense_date", "amount", "category", "subcategory",
    ):
        amount = money(row["total"])
        monthly[row["month"]]["administrative_expenses_total"] += amount
        if row["month"] == start:
            category_amounts[(row["category"], row["subcategory"])] = amount

    for row in _grouped_amounts(
        expenses.filter(payment_status=AdministrativeExpense.PaymentStatus.PAID,
                        paid_at__gte=first, paid_at__lt=end),
        "paid_at", "amount",
    ):
        monthly[row["month"]]["paid_administrative_expenses"] = money(row["total"])

    trend_rows = [{"month": _month_key(value), **_summary(monthly[value])} for value in months]
    summary = _summary(monthly[start])
    accrual_fields = [
        ("invoiced_sales", _("Hospital invoiced sales"), "#2563eb"),
        ("factory_purchases", _("Factory purchases"), "#f97316"),
        ("gross_profit", _("Estimated gross profit"), "#16a34a"),
        ("administrative_expenses_total", _("Company administrative expenses"), "#dc2626"),
        ("estimated_operating_balance", _("Estimated operating balance"), "#7c3aed"),
    ]
    cash_fields = [
        ("hospital_receipts", _("Registered hospital receipts"), "#0f766e"),
        ("factory_payments", _("Registered factory payments"), "#f97316"),
        ("paid_administrative_expenses", _("Paid administrative expenses"), "#dc2626"),
        ("estimated_net_cash_inflow", _("Estimated net cash inflow"), "#7c3aed"),
    ]
    # Decimal values serialize as strings. Number conversion is only for browser drawing.
    charts = {}
    for key, fields in (("accrual", accrual_fields), ("cash", cash_fields)):
        charts[key] = {
            "labels": [row["month"] for row in trend_rows],
            "series": [
                {"key": field, "label": label, "color": color,
                 "values": [row[field] for row in trend_rows],
                 "formatted": [format_money(row[field]) for row in trend_rows]}
                for field, label, color in fields
            ],
        }

    table_rows = [
        {"month": row["month"],
         "accrual": [format_money(row[field]) for field, _label, _color in accrual_fields],
         "cash": [format_money(row[field]) for field, _label, _color in cash_fields]}
        for row in trend_rows
    ]
    return {
        "month": selected_month, "period_start": start, "period_end": end,
        "trend_start": _month_key(first), "summary": summary, "trend_rows": trend_rows,
        "kpi_groups": _kpi_groups(summary), "chart_data": charts,
        "categories": _category_rows(category_amounts, selected_month, summary["administrative_expenses_total"]),
        "table_rows": table_rows,
        "accrual_labels": [label for _field, label, _color in accrual_fields],
        "cash_labels": [label for _field, label, _color in cash_fields],
        "generated_at": timezone.now(),
    }
