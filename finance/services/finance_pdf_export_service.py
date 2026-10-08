"""On-demand reports; never save files, create documents or recalculate business data."""
from pathlib import Path

from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import get_language, gettext as _

from .finance_pdf_charts import line_chart_svg
from .operating_finance_service import build_operating_finance_context
from .settlement_finance_service import build_settlement_finance_dashboard_data, format_money

ORDER_LIMIT = 100
RISK_LIMIT = 50
PRINT_CSS = Path(__file__).resolve().parents[1] / "static" / "finance" / "css" / "finance_report_pdf.css"


class FinancePDFError(RuntimeError):
    pass


def deny_resource_fetch(url, *args, **kwargs):
    # CSS is supplied as text and SVG is inline; no URL fetch is ever needed.
    raise ValueError("External and file resource loading is disabled for finance reports.")


def render_report_pdf(template, context):
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
        import fitz
        font_config = FontConfiguration()
        html = render_to_string(template, {"LANGUAGE_CODE": get_language(), **context})
        pdf = HTML(string=html, url_fetcher=deny_resource_fetch).write_pdf(
            stylesheets=[CSS(string=PRINT_CSS.read_text(encoding="utf-8"), url_fetcher=deny_resource_fetch)],
            font_config=font_config,
        )
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Renderer did not return a PDF.")
        with fitz.open(stream=pdf, filetype="pdf") as document:
            if not document.page_count:
                raise ValueError("Renderer returned a report without pages.")
        return pdf
    except Exception as exc:
        raise FinancePDFError("Unable to render finance report.") from exc


def _cards(cards):
    return [{**card, "label": _(card["label"]), "negative": str(card["value"]).startswith("-")} for card in cards]


def _chart(title, labels, series):
    return {
        "title": title, "svg": line_chart_svg(labels, series, title),
        "headers": [item["label"] for item in series],
        "rows": [{"month": label, "amounts": [
            item.get("formatted", [])[index] if item.get("formatted") else format_money(item["values"][index])
            for item in series]} for index, label in enumerate(labels)],
    }


def _record_scope(shown, total, limit):
    return _("Records shown: %(shown)s of %(total)s (limit %(limit)s).") % {"shown": shown, "total": total, "limit": limit}


def build_settlement_report_context(*, filter_values, parsed_filters):
    data = build_settlement_finance_dashboard_data(reporting_currency="EUR", **parsed_filters)
    accrual, cash = data["chart_data"]["accrual"], data["chart_data"]["cash"]
    filters = [
        {"label": label, "value": filter_values.get(field) or _("All")}
        for field, label in (("date_from", _("Issue date from")), ("date_to", _("Issue date to")),
            ("hospital", _("Hospital")), ("factory", _("Factory")), ("order", _("Order")),
            ("status", _("Settlement status")))
    ]
    from settlements.models import SettlementAccount
    filters[-1]["value"] = _(dict(SettlementAccount.Status.choices).get(filter_values.get("status"), _("All")))
    orders = []
    for row in data["order_rows"][:ORDER_LIMIT]:
        display = dict(row)
        for field in ("invoice_number_display", "po_number_display"):
            if len(display[field]) > 180:
                display[field] = display[field][:180] + " [...]"
        orders.append(display)
    overdue, due_soon = data["overdue_receivables"][:RISK_LIMIT], data["due_soon_receivables"][:RISK_LIMIT]
    return {
        "report_title": _("Financial Actuals Report"), "currency": "EUR", "generated_at": data["generated_at"],
        "filters": filters, "kpi_cards": _cards(data["kpi_cards"]),
        "charts": [
            _chart(_("Sales and purchase trends"), accrual["labels"], [
                {"label": _("Hospital invoiced sales"), "values": accrual["sales"]},
                {"label": _("Factory purchases"), "values": accrual["purchases"]},
                {"label": _("Estimated gross profit"), "values": accrual["gross_profit"]},
            ]),
            _chart(_("Cash flow trends"), cash["labels"], [
                {"label": _("Registered hospital receipts"), "values": cash["receipts"]},
                {"label": _("Registered factory payments"), "values": cash["payments"]},
                {"label": _("Net cash inflow"), "values": cash["net_inflow"]},
            ]),
        ],
        "risks": [
            {"title": _("Overdue receivables"), "rows": overdue,
             "scope": _record_scope(len(overdue), len(data["overdue_receivables"]), RISK_LIMIT)},
            {"title": _("Receivables due within 30 days"), "rows": due_soon,
             "scope": _record_scope(len(due_soon), len(data["due_soon_receivables"]), RISK_LIMIT)},
        ],
        "order_rows": orders, "order_count": len(data["order_rows"]),
        "order_scope": _record_scope(len(orders), len(data["order_rows"]), ORDER_LIMIT),
        "account_count": data["summary"]["account_count"], "transaction_count": data["summary"]["transaction_count"],
    }


def build_operating_report_context(*, month=None):
    data = build_operating_finance_context(month=month)
    # Explicitly select report fields. Category URLs are not put in the PDF.
    categories = [
        {"label": row["label"], "amount_display": row["amount_display"], "share_display": row["share_display"],
         "children": [{"label": child["label"], "amount_display": child["amount_display"],
                       "share_display": child["share_display"]} for child in row["children"]]}
        for row in data["categories"]
    ]
    return {
        "report_title": _("Company Operating Analysis"), "currency": "EUR", "generated_at": data["generated_at"],
        "month": data["month"], "trend_start": data["trend_start"],
        "kpi_groups": [{**group, "cards": _cards(group["cards"])} for group in data["kpi_groups"]],
        "categories": categories,
        "charts": [
            _chart(_("Operating trends"), data["chart_data"]["accrual"]["labels"], data["chart_data"]["accrual"]["series"]),
            _chart(_("Cash flow trends"), data["chart_data"]["cash"]["labels"], data["chart_data"]["cash"]["series"]),
        ],
    }


def build_settlement_pdf(*, filter_values, parsed_filters):
    context = build_settlement_report_context(filter_values=filter_values, parsed_filters=parsed_filters)
    return {"content": render_report_pdf("finance/pdf/settlement_report.html", context),
            "filename": "Acoeurs_Financial_Actuals_" + timezone.localtime(context["generated_at"]).strftime("%Y%m%d") + ".pdf"}


def build_operating_pdf(*, month=None):
    context = build_operating_report_context(month=month)
    return {"content": render_report_pdf("finance/pdf/operating_report.html", context),
            "filename": "Acoeurs_Operating_Analysis_" + context["month"] + ".pdf"}
