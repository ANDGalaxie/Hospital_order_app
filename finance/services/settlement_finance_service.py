from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value):
    if value is None:
        return ZERO

    try:
        decimal_value = Decimal(str(value))
    except (TypeError, ValueError):
        return ZERO

    return decimal_value.quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


def format_money(
    value,
    currency="EUR",
):
    value = money(value)

    text = f"{value:,.2f}".replace(
        ",",
        " ",
    )

    if currency == "EUR":
        return f"{text} €"

    return f"{text} {currency}"


def format_percent(value):
    value = Decimal(str(value or ZERO))

    return (
        value
        .quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
        * Decimal("100")
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def get_order_party_names(
    order,
    account,
):
    hospital_name = ""

    if getattr(
        order,
        "hospital",
        None,
    ):
        hospital_name = (
            order.hospital.name
        )

    if not hospital_name:
        hospital_name = (
            getattr(
                order,
                "hospital_name",
                "",
            )
            or ""
        )

    factory_name = ""

    if getattr(
        order,
        "factory",
        None,
    ):
        factory_name = (
            order.factory.name
        )

    if (
        account.direction
        == SettlementAccount
        .Direction
        .RECEIVABLE
        and not hospital_name
    ):
        hospital_name = (
            account.counterparty_name
        )

    if (
        account.direction
        == SettlementAccount
        .Direction
        .PAYABLE
        and not factory_name
    ):
        factory_name = (
            account.counterparty_name
        )

    return (
        hospital_name or "—",
        factory_name or "—",
    )


def build_settlement_finance_dashboard_data(
    *,
    reporting_currency="EUR",
    today=None,
):
    """
    根据正式结算账户和有效收付款流水，
    构造财务实绩 Dashboard 数据。

    不读取当前产品价格，不重新计算文档金额。

    统计范围：
    - 排除已取消的结算账户
    - 排除已冲销的收付款流水
    - 第一版仅统计指定记账币种
    """
    today = (
        today
        or timezone.localdate()
    )

    due_soon_end = (
        today
        + timedelta(days=30)
    )

    accounts = list(
        SettlementAccount.objects
        .exclude(
            status=(
                SettlementAccount
                .Status
                .CANCELLED
            )
        )
        .filter(
            currency=reporting_currency
        )
        .select_related(
            "document",
            "document__order",
            "document__order__hospital",
            "document__order__factory",
        )
        .order_by(
            "issue_date",
            "id",
        )
    )

    account_ids = [
        account.id
        for account in accounts
    ]

    transactions = list(
        PaymentTransaction.objects
        .filter(
            account_id__in=account_ids,
            status=(
                PaymentTransaction
                .Status
                .POSTED
            ),
        )
        .select_related(
            "account",
            "account__document",
            "account__document__order",
        )
        .order_by(
            "payment_date",
            "id",
        )
    )

    posted_by_account = defaultdict(
        lambda: ZERO
    )

    monthly_cash = defaultdict(
        lambda: {
            "receipts": ZERO,
            "payments": ZERO,
        }
    )

    receipt_total = ZERO
    payment_total = ZERO

    for transaction in transactions:
        amount = money(
            transaction.amount
        )

        posted_by_account[
            transaction.account_id
        ] += amount

        month_key = (
            transaction.payment_date
            .strftime("%Y-%m")
        )

        if (
            transaction.account.direction
            == SettlementAccount
            .Direction
            .RECEIVABLE
        ):
            receipt_total += amount

            monthly_cash[
                month_key
            ]["receipts"] += amount

        elif (
            transaction.account.direction
            == SettlementAccount
            .Direction
            .PAYABLE
        ):
            payment_total += amount

            monthly_cash[
                month_key
            ]["payments"] += amount

    sales_total = ZERO
    purchase_total = ZERO

    receivable_remaining = ZERO
    payable_remaining = ZERO

    overdue_receivable_total = ZERO
    due_soon_receivable_total = ZERO

    monthly_accrual = defaultdict(
        lambda: {
            "sales": ZERO,
            "purchases": ZERO,
        }
    )

    order_summary = {}

    overdue_receivables = []
    due_soon_receivables = []

    for account in accounts:
        document = account.document
        order = document.order

        original_amount = money(
            account.original_amount
        )

        posted_amount = money(
            posted_by_account[
                account.id
            ]
        )

        remaining_amount = money(
            original_amount
            - posted_amount
        )

        if remaining_amount < ZERO:
            remaining_amount = ZERO

        if account.issue_date:
            month_key = (
                account.issue_date
                .strftime("%Y-%m")
            )
        else:
            month_key = "未指定日期"

        hospital_name, factory_name = (
            get_order_party_names(
                order,
                account,
            )
        )

        row = order_summary.setdefault(
            order.id,
            {
                "order_id": order.id,
                "order_number": str(
                    order.bon_de_commande
                ),
                "hospital_name": (
                    hospital_name
                ),
                "factory_name": (
                    factory_name
                ),
                "invoice_numbers": [],
                "po_numbers": [],
                "sales": ZERO,
                "purchases": ZERO,
                "received": ZERO,
                "paid": ZERO,
                "receivable_remaining": ZERO,
                "payable_remaining": ZERO,
            },
        )

        if hospital_name != "—":
            row["hospital_name"] = (
                hospital_name
            )

        if factory_name != "—":
            row["factory_name"] = (
                factory_name
            )

        if (
            account.direction
            == SettlementAccount
            .Direction
            .RECEIVABLE
        ):
            sales_total += original_amount
            receivable_remaining += (
                remaining_amount
            )

            monthly_accrual[
                month_key
            ]["sales"] += original_amount

            row["invoice_numbers"].append(
                document.document_number
            )

            row["sales"] += original_amount
            row["received"] += posted_amount
            row[
                "receivable_remaining"
            ] += remaining_amount

            risk_row = {
                "account_id": account.id,
                "order_id": order.id,
                "order_number": str(
                    order.bon_de_commande
                ),
                "document_number": (
                    document.document_number
                ),
                "counterparty_name": (
                    account.counterparty_name
                ),
                "due_date": account.due_date,
                "remaining_amount": (
                    remaining_amount
                ),
                "remaining_display": (
                    format_money(
                        remaining_amount,
                        reporting_currency,
                    )
                ),
            }

            if (
                remaining_amount > ZERO
                and account.due_date
                and account.due_date < today
            ):
                overdue_receivable_total += (
                    remaining_amount
                )

                overdue_receivables.append(
                    risk_row
                )

            elif (
                remaining_amount > ZERO
                and account.due_date
                and today
                <= account.due_date
                <= due_soon_end
            ):
                due_soon_receivable_total += (
                    remaining_amount
                )

                due_soon_receivables.append(
                    risk_row
                )

        elif (
            account.direction
            == SettlementAccount
            .Direction
            .PAYABLE
        ):
            purchase_total += original_amount
            payable_remaining += (
                remaining_amount
            )

            monthly_accrual[
                month_key
            ]["purchases"] += (
                original_amount
            )

            row["po_numbers"].append(
                document.document_number
            )

            row["purchases"] += (
                original_amount
            )

            row["paid"] += posted_amount

            row[
                "payable_remaining"
            ] += remaining_amount

    gross_profit = money(
        sales_total
        - purchase_total
    )

    cash_net_inflow = money(
        receipt_total
        - payment_total
    )

    if sales_total > ZERO:
        gross_margin = (
            gross_profit
            / sales_total
        )
    else:
        gross_margin = ZERO

    order_rows = []

    for row in order_summary.values():
        row["gross_profit"] = money(
            row["sales"]
            - row["purchases"]
        )

        row["invoice_number_display"] = (
            ", ".join(
                row["invoice_numbers"]
            )
            or "—"
        )

        row["po_number_display"] = (
            ", ".join(
                row["po_numbers"]
            )
            or "—"
        )

        for field_name in [
            "sales",
            "purchases",
            "gross_profit",
            "received",
            "paid",
            "receivable_remaining",
            "payable_remaining",
        ]:
            row[
                f"{field_name}_display"
            ] = format_money(
                row[field_name],
                reporting_currency,
            )

        order_rows.append(row)

    order_rows.sort(
        key=lambda row: (
            row["order_number"]
        ),
        reverse=True,
    )

    accrual_months = sorted(
        monthly_accrual.keys()
    )

    cash_months = sorted(
        monthly_cash.keys()
    )

    accrual_chart = {
        "labels": accrual_months,
        "sales": [],
        "purchases": [],
        "gross_profit": [],
    }

    for month_key in accrual_months:
        sales = money(
            monthly_accrual[
                month_key
            ]["sales"]
        )

        purchases = money(
            monthly_accrual[
                month_key
            ]["purchases"]
        )

        accrual_chart[
            "sales"
        ].append(float(sales))

        accrual_chart[
            "purchases"
        ].append(float(purchases))

        accrual_chart[
            "gross_profit"
        ].append(
            float(
                money(
                    sales
                    - purchases
                )
            )
        )

    cash_chart = {
        "labels": cash_months,
        "receipts": [],
        "payments": [],
        "net_inflow": [],
    }

    for month_key in cash_months:
        receipts = money(
            monthly_cash[
                month_key
            ]["receipts"]
        )

        payments = money(
            monthly_cash[
                month_key
            ]["payments"]
        )

        cash_chart[
            "receipts"
        ].append(float(receipts))

        cash_chart[
            "payments"
        ].append(float(payments))

        cash_chart[
            "net_inflow"
        ].append(
            float(
                money(
                    receipts
                    - payments
                )
            )
        )

    overdue_receivables.sort(
        key=lambda row: (
            row["due_date"],
            -row["remaining_amount"],
        )
    )

    due_soon_receivables.sort(
        key=lambda row: (
            row["due_date"],
            -row["remaining_amount"],
        )
    )

    summary = {
        "sales_total": money(
            sales_total
        ),
        "purchase_total": money(
            purchase_total
        ),
        "gross_profit": gross_profit,
        "gross_margin": gross_margin,
        "receipt_total": money(
            receipt_total
        ),
        "payment_total": money(
            payment_total
        ),
        "cash_net_inflow": (
            cash_net_inflow
        ),
        "receivable_remaining": money(
            receivable_remaining
        ),
        "payable_remaining": money(
            payable_remaining
        ),
        "overdue_receivable_total": money(
            overdue_receivable_total
        ),
        "due_soon_receivable_total": money(
            due_soon_receivable_total
        ),
        "account_count": len(accounts),
        "transaction_count": len(
            transactions
        ),
    }

    kpi_cards = [
        {
            "key": "sales",
            "label": "医院开票销售额",
            "value": format_money(
                sales_total,
                reporting_currency,
            ),
            "help": (
                "有效医院 Invoice "
                "结算账户原始金额合计。"
            ),
        },
        {
            "key": "purchases",
            "label": "工厂采购额",
            "value": format_money(
                purchase_total,
                reporting_currency,
            ),
            "help": (
                "有效 Factory PO "
                "结算账户原始金额合计。"
            ),
        },
        {
            "key": "gross_profit",
            "label": "预计毛利润",
            "value": format_money(
                gross_profit,
                reporting_currency,
            ),
            "help": (
                "医院开票销售额减去"
                "工厂采购额。"
            ),
        },
        {
            "key": "gross_margin",
            "label": "预计毛利率",
            "value": (
                f"{format_percent(gross_margin)}%"
            ),
            "help": (
                "预计毛利润除以"
                "医院开票销售额。"
            ),
        },
        {
            "key": "receipts",
            "label": "医院已收金额",
            "value": format_money(
                receipt_total,
                reporting_currency,
            ),
            "help": (
                "有效医院收款流水合计。"
            ),
        },
        {
            "key": "payments",
            "label": "工厂已付金额",
            "value": format_money(
                payment_total,
                reporting_currency,
            ),
            "help": (
                "有效工厂付款流水合计。"
            ),
        },
        {
            "key": "cash_net",
            "label": "实际现金净流入",
            "value": format_money(
                cash_net_inflow,
                reporting_currency,
            ),
            "help": (
                "医院已收金额减去"
                "工厂已付金额。"
            ),
        },
        {
            "key": "receivable_remaining",
            "label": "医院待收余额",
            "value": format_money(
                receivable_remaining,
                reporting_currency,
            ),
            "help": (
                "医院应收原始金额减去"
                "有效收款。"
            ),
        },
        {
            "key": "payable_remaining",
            "label": "工厂待付余额",
            "value": format_money(
                payable_remaining,
                reporting_currency,
            ),
            "help": (
                "工厂应付原始金额减去"
                "有效付款。"
            ),
        },
        {
            "key": "overdue",
            "label": "逾期应收金额",
            "value": format_money(
                overdue_receivable_total,
                reporting_currency,
            ),
            "help": (
                "截止日期早于今天且"
                "仍有余额的医院应收。"
            ),
        },
    ]

    return {
        "reporting_currency": (
            reporting_currency
        ),
        "today": today,
        "summary": summary,
        "kpi_cards": kpi_cards,
        "chart_data": {
            "accrual": accrual_chart,
            "cash": cash_chart,
        },
        "order_rows": order_rows,
        "overdue_receivables": (
            overdue_receivables
        ),
        "due_soon_receivables": (
            due_soon_receivables
        ),
        "generated_at": timezone.now(),
    }
