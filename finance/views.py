from django.contrib.auth.decorators import (
    login_required,
)
from django.shortcuts import render
from django.utils.dateparse import parse_date

from finance.services.settlement_finance_service import (
    build_settlement_finance_dashboard_data,
    format_money,
)
from portal.services.common import (
    get_portal_lang,
    get_user_display_name,
)


def build_accrual_rows(
    chart_data,
    reporting_currency,
):
    accrual = chart_data["accrual"]

    rows = []

    for index, month in enumerate(
        accrual["labels"]
    ):
        rows.append(
            {
                "month": month,
                "sales": format_money(
                    accrual["sales"][index],
                    reporting_currency,
                ),
                "purchases": format_money(
                    accrual[
                        "purchases"
                    ][index],
                    reporting_currency,
                ),
                "gross_profit": format_money(
                    accrual[
                        "gross_profit"
                    ][index],
                    reporting_currency,
                ),
            }
        )

    return rows


def build_cash_rows(
    chart_data,
    reporting_currency,
):
    cash = chart_data["cash"]

    rows = []

    for index, month in enumerate(
        cash["labels"]
    ):
        rows.append(
            {
                "month": month,
                "receipts": format_money(
                    cash["receipts"][index],
                    reporting_currency,
                ),
                "payments": format_money(
                    cash["payments"][index],
                    reporting_currency,
                ),
                "net_inflow": format_money(
                    cash[
                        "net_inflow"
                    ][index],
                    reporting_currency,
                ),
            }
        )

    return rows


def parse_filter_date(
    raw_value,
    label,
    errors,
):
    raw_value = str(
        raw_value or ""
    ).strip()

    if not raw_value:
        return None

    parsed_value = parse_date(
        raw_value
    )

    if parsed_value is None:
        errors.append(
            f"{label}格式无效，"
            "请使用 YYYY-MM-DD。"
        )

    return parsed_value


@login_required
def settlement_dashboard(request):
    filter_errors = []

    date_from_raw = request.GET.get(
        "date_from",
        "",
    )

    date_to_raw = request.GET.get(
        "date_to",
        "",
    )

    hospital_query = request.GET.get(
        "hospital",
        "",
    ).strip()

    factory_query = request.GET.get(
        "factory",
        "",
    ).strip()

    order_query = request.GET.get(
        "order",
        "",
    ).strip()

    status = request.GET.get(
        "status",
        "",
    ).strip()

    date_from = parse_filter_date(
        date_from_raw,
        "开始日期",
        filter_errors,
    )

    date_to = parse_filter_date(
        date_to_raw,
        "结束日期",
        filter_errors,
    )

    if (
        date_from
        and date_to
        and date_from > date_to
    ):
        filter_errors.append(
            "开始日期不能晚于结束日期。"
        )

        date_from = None
        date_to = None

    dashboard_data = (
        build_settlement_finance_dashboard_data(
            reporting_currency="EUR",
            date_from=date_from,
            date_to=date_to,
            hospital_query=hospital_query,
            factory_query=factory_query,
            order_query=order_query,
            status=status,
        )
    )

    reporting_currency = dashboard_data[
        "reporting_currency"
    ]

    filter_values = {
        "date_from": date_from_raw,
        "date_to": date_to_raw,
        "hospital": hospital_query,
        "factory": factory_query,
        "order": order_query,
        "status": status,
    }

    has_active_filters = any(
        str(value or "").strip()
        for value in filter_values.values()
    )

    context = {
        **dashboard_data,
        "lang": get_portal_lang(
            request
        ),
        "user_display_name": (
            get_user_display_name(
                request.user
            )
        ),
        "filter_values": filter_values,
        "filter_errors": filter_errors,
        "has_active_filters": (
            has_active_filters
        ),
        "accrual_rows": (
            build_accrual_rows(
                dashboard_data[
                    "chart_data"
                ],
                reporting_currency,
            )
        ),
        "cash_rows": (
            build_cash_rows(
                dashboard_data[
                    "chart_data"
                ],
                reporting_currency,
            )
        ),
    }

    return render(
        request,
        (
            "finance/"
            "settlement_dashboard.html"
        ),
        context,
    )
