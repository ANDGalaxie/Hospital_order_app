from django.utils.translation import gettext as _
from portal.i18n import display_choices
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.dateparse import parse_date

from finance.services.finance_export_service import (
    build_finance_export_xlsx,
)
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
            _('%(value1)s格式无效，请使用 YYYY-MM-DD。') % {'value1': label}
        )

    return parsed_value


def parse_finance_filters(request):
    errors = []

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
        _("开始日期"),
        errors,
    )

    date_to = parse_filter_date(
        date_to_raw,
        _("结束日期"),
        errors,
    )

    if (
        date_from
        and date_to
        and date_from > date_to
    ):
        errors.append(
            _("开始日期不能晚于结束日期。")
        )

        date_from = None
        date_to = None

    filter_values = {
        "date_from": date_from_raw,
        "date_to": date_to_raw,
        "hospital": hospital_query,
        "factory": factory_query,
        "order": order_query,
        "status": status,
    }

    parsed_filters = {
        "date_from": date_from,
        "date_to": date_to,
        "hospital_query": (
            hospital_query
        ),
        "factory_query": (
            factory_query
        ),
        "order_query": order_query,
        "status": status,
    }

    return {
        "errors": errors,
        "filter_values": filter_values,
        "parsed_filters": (
            parsed_filters
        ),
        "has_active_filters": any(
            str(value or "").strip()
            for value in (
                filter_values.values()
            )
        ),
    }


@staff_member_required
def settlement_dashboard(request):
    filter_state = (
        parse_finance_filters(
            request
        )
    )

    dashboard_data = (
        build_settlement_finance_dashboard_data(
            reporting_currency="EUR",
            **filter_state[
                "parsed_filters"
            ],
        )
    )

    dashboard_data["kpi_cards"] = [
        {**card, "label": _(card["label"]), "help": _(card["help"])}
        for card in dashboard_data["kpi_cards"]
    ]
    dashboard_data["status_choices"] = display_choices(dashboard_data["status_choices"])
    dashboard_data["chart_data"]["ui"] = {
        "empty": _("暂无趋势数据"),
        "sales": _("销售额"),
        "purchases": _("采购额"),
        "gross_profit": _("预计毛利润"),
        "receipts": _("医院收款"),
        "payments": _("工厂付款"),
        "net_inflow": _("现金净流入"),
    }

    reporting_currency = dashboard_data[
        "reporting_currency"
    ]

    query_string = (
        request.GET.urlencode()
    )

    if query_string:
        export_url = (
            "export.xlsx?"
            + query_string
        )
    else:
        export_url = "export.xlsx"

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
        "filter_values": (
            filter_state[
                "filter_values"
            ]
        ),
        "filter_errors": (
            filter_state["errors"]
        ),
        "has_active_filters": (
            filter_state[
                "has_active_filters"
            ]
        ),
        "export_url": export_url,
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


@staff_member_required
def settlement_dashboard_export(
    request,
):
    filter_state = (
        parse_finance_filters(
            request
        )
    )

    if filter_state["errors"]:
        return HttpResponse(
            "\n".join(
                filter_state["errors"]
            ),
            status=400,
            content_type=(
                "text/plain; charset=utf-8"
            ),
        )

    export_result = (
        build_finance_export_xlsx(
            reporting_currency="EUR",
            **filter_state[
                "parsed_filters"
            ],
        )
    )

    response = HttpResponse(
        export_result["content"],
        content_type=(
            "application/vnd."
            "openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )

    response[
        "Content-Disposition"
    ] = (
        'attachment; filename="'
        + export_result["filename"]
        + '"'
    )

    response[
        "X-Content-Type-Options"
    ] = "nosniff"

    return response
