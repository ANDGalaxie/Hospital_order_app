from django.contrib.auth.decorators import login_required
from django.shortcuts import render

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
                    accrual["purchases"][index],
                    reporting_currency,
                ),
                "gross_profit": format_money(
                    accrual["gross_profit"][index],
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
                    cash["net_inflow"][index],
                    reporting_currency,
                ),
            }
        )

    return rows


@login_required
def settlement_dashboard(request):
    dashboard_data = (
        build_settlement_finance_dashboard_data(
            reporting_currency="EUR"
        )
    )

    reporting_currency = dashboard_data[
        "reporting_currency"
    ]

    context = {
        **dashboard_data,
        "lang": get_portal_lang(request),
        "user_display_name": (
            get_user_display_name(
                request.user
            )
        ),
        "accrual_rows": (
            build_accrual_rows(
                dashboard_data["chart_data"],
                reporting_currency,
            )
        ),
        "cash_rows": (
            build_cash_rows(
                dashboard_data["chart_data"],
                reporting_currency,
            )
        ),
    }

    return render(
        request,
        "finance/settlement_dashboard.html",
        context,
    )
