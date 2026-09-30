from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from documents.models import GeneratedDocument
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


ZERO_MONEY = Decimal("0.00")

PAYABLE_SORT_CHOICES = (
    ("payment_priority", "待付款优先"),
    ("arrival_asc", "预计到货：早 → 晚"),
    ("generated_desc", "文件生成：新 → 旧"),
    ("issue_desc", "开立日期：新 → 旧"),
    ("remaining_desc", "待付金额：高 → 低"),
)

STATUS_LABELS = dict(
    SettlementAccount.Status.choices
)

STATUS_CLASSES = {
    SettlementAccount.Status.UNPAID: "unpaid",
    SettlementAccount.Status.PARTIALLY_PAID: "partial",
    SettlementAccount.Status.PAID: "paid",
    SettlementAccount.Status.OVERDUE: "overdue",
    SettlementAccount.Status.CANCELLED: "cancelled",
}


def parse_iso_date(value):
    value = str(value or "").strip()

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return None


def get_payable_expected_arrival(document):
    """Read the saved Factory PO arrival date without deriving or persisting it."""
    if document.document_type != GeneratedDocument.DocumentType.FACTORY_PO:
        return None

    data = document.source_data
    if not isinstance(data, dict):
        return None

    # As in the document center, older snapshots may store the payload directly.
    payload = data.get("po_data")
    if not isinstance(payload, dict):
        payload = data

    po = payload.get("po")
    if not isinstance(po, dict):
        return None

    return parse_iso_date(po.get("expected_arrival_iso"))


def sort_payable_accounts(accounts, sort):
    """Sort decorated, filtered accounts before pagination."""
    def arrival_key(account):
        arrival = account.portal_expected_arrival
        return (arrival is None, arrival or date.max)

    # Stable sorts retain descending IDs when all business keys are equal.
    accounts.sort(key=lambda account: account.id, reverse=True)

    if sort == "issue_desc":
        accounts.sort(key=lambda account: account.issue_date, reverse=True)
    elif sort == "remaining_desc":
        accounts.sort(
            key=lambda account: (
                -account.portal_remaining_amount,
                *arrival_key(account),
            )
        )
    else:
        accounts.sort(
            key=lambda account: account.portal_generated_at,
            reverse=True,
        )
        if sort == "arrival_asc":
            accounts.sort(key=arrival_key)
        elif sort == "payment_priority":
            accounts.sort(
                key=lambda account: (
                    not (
                        account.portal_remaining_amount > ZERO_MONEY
                        and account.portal_status
                        != SettlementAccount.Status.CANCELLED
                    ),
                    *arrival_key(account),
                )
            )


def get_account_queryset(direction=None):
    posted_transactions = (
        PaymentTransaction.objects
        .filter(
            status=(
                PaymentTransaction
                .Status
                .POSTED
            )
        )
        .order_by(
            "payment_date",
            "id",
        )
    )

    queryset = (
        SettlementAccount.objects
        .select_related(
            "document",
            "document__order",
            "document__shipment_batch",
        )
        .prefetch_related(
            Prefetch(
                "transactions",
                queryset=posted_transactions,
                to_attr=(
                    "portal_posted_transactions"
                ),
            )
        )
        .order_by(
            "-issue_date",
            "-id",
        )
    )

    if direction:
        queryset = queryset.filter(
            direction=direction
        )

    return queryset


def calculate_prefetched_posted_amount(
    account,
):
    transactions = getattr(
        account,
        "portal_posted_transactions",
        [],
    )

    total = sum(
        (
            Decimal(
                transaction.amount
            )
            for transaction in transactions
        ),
        ZERO_MONEY,
    )

    return total.quantize(
        Decimal("0.01")
    )


def calculate_effective_status(
    account,
    posted_amount,
):
    if (
        account.status
        == SettlementAccount
        .Status
        .CANCELLED
    ):
        return (
            SettlementAccount
            .Status
            .CANCELLED
        )

    if (
        posted_amount
        >= account.original_amount
    ):
        return (
            SettlementAccount
            .Status
            .PAID
        )

    if (
        account.due_date
        and account.due_date
        < timezone.localdate()
    ):
        return (
            SettlementAccount
            .Status
            .OVERDUE
        )

    if posted_amount > ZERO_MONEY:
        return (
            SettlementAccount
            .Status
            .PARTIALLY_PAID
        )

    return SettlementAccount.Status.UNPAID


def decorate_account(account):
    posted_amount = (
        calculate_prefetched_posted_amount(
            account
        )
    )

    remaining_amount = max(
        Decimal(account.original_amount)
        - posted_amount,
        ZERO_MONEY,
    ).quantize(
        Decimal("0.01")
    )

    effective_status = (
        calculate_effective_status(
            account,
            posted_amount,
        )
    )

    original_amount = Decimal(
        account.original_amount
    )

    if original_amount > ZERO_MONEY:
        progress = int(
            (
                posted_amount
                / original_amount
            )
            * 100
        )
    else:
        progress = 0

    progress = max(
        0,
        min(progress, 100),
    )

    document = account.document
    account.portal_generated_at = document.generated_at
    account.portal_expected_arrival = (
        get_payable_expected_arrival(document)
        if account.direction == SettlementAccount.Direction.PAYABLE
        else None
    )
    order = document.order
    batch = document.shipment_batch

    account.portal_posted_amount = (
        posted_amount
    )

    account.portal_remaining_amount = (
        remaining_amount
    )

    account.portal_status = (
        effective_status
    )

    account.portal_status_label = (
        STATUS_LABELS.get(
            effective_status,
            effective_status,
        )
    )

    account.portal_status_class = (
        STATUS_CLASSES.get(
            effective_status,
            "unpaid",
        )
    )

    account.portal_progress = progress

    account.portal_detail_url = reverse(
        "portal:settlement_account_detail",
        args=[account.id],
    )

    account.portal_document_url = reverse(
        "portal:document_detail",
        args=[document.id],
    )

    account.portal_order_url = reverse(
        "portal:order_detail",
        args=[order.id],
    )

    account.portal_order_number = (
        order.bon_de_commande
    )

    account.portal_batch_label = (
        f"Batch {batch.batch_number}"
        if batch
        else "—"
    )

    account.portal_direction_label = (
        account.get_direction_display()
    )

    return account


def summarize_accounts(accounts):
    return {
        "count": len(accounts),
        "original_total": sum(
            (
                Decimal(
                    account.original_amount
                )
                for account in accounts
            ),
            ZERO_MONEY,
        ),
        "posted_total": sum(
            (
                account.portal_posted_amount
                for account in accounts
            ),
            ZERO_MONEY,
        ),
        "remaining_total": sum(
            (
                account.portal_remaining_amount
                for account in accounts
            ),
            ZERO_MONEY,
        ),
        "overdue_count": sum(
            1
            for account in accounts
            if (
                account.portal_status
                == SettlementAccount
                .Status
                .OVERDUE
            )
        ),
    }


def build_settlement_home_context(request):
    accounts = [
        decorate_account(account)
        for account in get_account_queryset()
    ]

    receivables = [
        account
        for account in accounts
        if (
            account.direction
            == SettlementAccount
            .Direction
            .RECEIVABLE
        )
    ]

    payables = [
        account
        for account in accounts
        if (
            account.direction
            == SettlementAccount
            .Direction
            .PAYABLE
        )
    ]

    receivable_summary = (
        summarize_accounts(
            receivables
        )
    )

    payable_summary = (
        summarize_accounts(
            payables
        )
    )

    transaction_count = (
        PaymentTransaction.objects.count()
    )

    posted_transaction_count = (
        PaymentTransaction.objects
        .filter(
            status=(
                PaymentTransaction
                .Status
                .POSTED
            )
        )
        .count()
    )

    cards = [
        {
            "title": "医院应收",
            "subtitle": "Hospital Receivables",
            "description": (
                "查看医院发票、到期日期、"
                "已收金额和待收余额。"
            ),
            "symbol": "AR",
            "theme": "receivable",
            "url": reverse(
                "portal:settlement_receivables"
            ),
            "count": (
                receivable_summary["count"]
            ),
            "amount_label": "待收余额",
            "amount": (
                receivable_summary[
                    "remaining_total"
                ]
            ),
        },
        {
            "title": "工厂应付",
            "subtitle": "Factory Payables",
            "description": (
                "查看工厂 PO、应付金额、"
                "已付金额和待付余额。"
            ),
            "symbol": "AP",
            "theme": "payable",
            "url": reverse(
                "portal:settlement_payables"
            ),
            "count": (
                payable_summary["count"]
            ),
            "amount_label": "待付余额",
            "amount": (
                payable_summary[
                    "remaining_total"
                ]
            ),
        },
        {
            "title": "收付款流水",
            "subtitle": "Payment Transactions",
            "description": (
                "查看全部有效流水和"
                "已冲销的收付款记录。"
            ),
            "symbol": "TXN",
            "theme": "transaction",
            "url": reverse(
                "portal:settlement_transactions"
            ),
            "count": transaction_count,
            "amount_label": "有效流水",
            "amount": posted_transaction_count,
            "amount_is_money": False,
        },
    ]

    return {
        "cards": cards,
        "receivable_summary": (
            receivable_summary
        ),
        "payable_summary": (
            payable_summary
        ),
        "transaction_count": (
            transaction_count
        ),
        "posted_transaction_count": (
            posted_transaction_count
        ),
        "recent_accounts": accounts[:8],
    }


def build_account_list_context(
    request,
    *,
    direction,
):
    query = str(
        request.GET.get("q")
        or ""
    ).strip()

    status_filter = str(
        request.GET.get("status")
        or "all"
    ).strip()

    due_scope = str(
        request.GET.get("due")
        or "all"
    ).strip()

    issue_from_text = str(
        request.GET.get("issue_from")
        or ""
    ).strip()

    issue_to_text = str(
        request.GET.get("issue_to")
        or ""
    ).strip()

    queryset = get_account_queryset(
        direction=direction
    )

    if query:
        queryset = queryset.filter(
            Q(
                document__document_number__icontains=query
            )
            | Q(
                document__order__bon_de_commande__icontains=query
            )
            | Q(
                counterparty_name__icontains=query
            )
            | Q(
                notes__icontains=query
            )
        )

    issue_from = parse_iso_date(
        issue_from_text
    )

    issue_to = parse_iso_date(
        issue_to_text
    )

    if issue_from:
        queryset = queryset.filter(
            issue_date__gte=issue_from
        )

    if issue_to:
        queryset = queryset.filter(
            issue_date__lte=issue_to
        )

    accounts = [
        decorate_account(account)
        for account in queryset
    ]

    valid_statuses = {
        value
        for value, label
        in SettlementAccount
        .Status
        .choices
    }

    if status_filter in valid_statuses:
        accounts = [
            account
            for account in accounts
            if (
                account.portal_status
                == status_filter
            )
        ]

    today = timezone.localdate()
    thirty_days_later = (
        today + timedelta(days=30)
    )

    if due_scope == "overdue":
        accounts = [
            account
            for account in accounts
            if (
                account.portal_status
                == SettlementAccount
                .Status
                .OVERDUE
            )
        ]

    elif due_scope == "due_30":
        accounts = [
            account
            for account in accounts
            if (
                account.due_date
                and today
                <= account.due_date
                <= thirty_days_later
                and account.portal_remaining_amount
                > ZERO_MONEY
            )
        ]

    elif due_scope == "no_due":
        accounts = [
            account
            for account in accounts
            if account.due_date is None
        ]

    sort = "issue_desc"
    if direction == SettlementAccount.Direction.PAYABLE:
        sort = str(request.GET.get("sort") or "payment_priority").strip()
        if sort not in dict(PAYABLE_SORT_CHOICES):
            sort = "payment_priority"
        sort_payable_accounts(accounts, sort)

    summary = summarize_accounts(
        accounts
    )

    paginator = Paginator(
        accounts,
        25,
    )

    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    query_params = request.GET.copy()
    query_params.pop("page", None)

    is_receivable = (
        direction
        == SettlementAccount
        .Direction
        .RECEIVABLE
    )

    return {
        "page_obj": page_obj,
        "query_without_page": (
            query_params.urlencode()
        ),
        "query": query,
        "status_filter": status_filter,
        "due_scope": due_scope,
        "is_receivable": is_receivable,
        "sort": sort,
        "sort_choices": PAYABLE_SORT_CHOICES,
        "issue_from": issue_from_text,
        "issue_to": issue_to_text,
        "status_choices": (
            SettlementAccount
            .Status
            .choices
        ),
        "direction": direction,
        "page_title": (
            "医院应收"
            if is_receivable
            else "工厂应付"
        ),
        "page_subtitle": (
            "Hospital Receivables"
            if is_receivable
            else "Factory Payables"
        ),
        "counterparty_label": (
            "医院"
            if is_receivable
            else "工厂"
        ),
        "posted_label": (
            "已收"
            if is_receivable
            else "已付"
        ),
        "remaining_label": (
            "待收"
            if is_receivable
            else "待付"
        ),
        "list_url": reverse(
            (
                "portal:"
                "settlement_receivables"
            )
            if is_receivable
            else (
                "portal:"
                "settlement_payables"
            )
        ),
        "summary": summary,
    }


def decorate_transaction(transaction):
    account = transaction.account
    document = account.document
    order = document.order

    is_receivable = (
        account.direction
        == SettlementAccount
        .Direction
        .RECEIVABLE
    )

    transaction.portal_direction_label = (
        "收款"
        if is_receivable
        else "付款"
    )

    transaction.portal_direction_class = (
        "receivable"
        if is_receivable
        else "payable"
    )

    transaction.portal_counterparty = (
        account.counterparty_name
    )

    transaction.portal_document_url = (
        reverse(
            "portal:document_detail",
            args=[document.id],
        )
    )

    transaction.portal_order_url = (
        reverse(
            "portal:order_detail",
            args=[order.id],
        )
    )

    transaction.portal_order_number = (
        order.bon_de_commande
    )

    return transaction


def build_transaction_list_context(
    request,
):
    query = str(
        request.GET.get("q")
        or ""
    ).strip()

    direction_filter = str(
        request.GET.get("direction")
        or "all"
    ).strip()

    status_filter = str(
        request.GET.get("status")
        or "all"
    ).strip()

    method_filter = str(
        request.GET.get("method")
        or "all"
    ).strip()

    date_from_text = str(
        request.GET.get("date_from")
        or ""
    ).strip()

    date_to_text = str(
        request.GET.get("date_to")
        or ""
    ).strip()

    queryset = (
        PaymentTransaction.objects
        .select_related(
            "account",
            "account__document",
            "account__document__order",
            "created_by",
            "reversed_by",
        )
        .order_by(
            "-payment_date",
            "-id",
        )
    )

    if query:
        queryset = queryset.filter(
            Q(
                account__document__document_number__icontains=query
            )
            | Q(
                account__document__order__bon_de_commande__icontains=query
            )
            | Q(
                account__counterparty_name__icontains=query
            )
            | Q(
                reference__icontains=query
            )
            | Q(
                notes__icontains=query
            )
        )

    valid_directions = {
        value
        for value, label
        in SettlementAccount
        .Direction
        .choices
    }

    if direction_filter in valid_directions:
        queryset = queryset.filter(
            account__direction=(
                direction_filter
            )
        )

    valid_statuses = {
        value
        for value, label
        in PaymentTransaction
        .Status
        .choices
    }

    if status_filter in valid_statuses:
        queryset = queryset.filter(
            status=status_filter
        )

    valid_methods = {
        value
        for value, label
        in PaymentTransaction
        .Method
        .choices
    }

    if method_filter in valid_methods:
        queryset = queryset.filter(
            method=method_filter
        )

    date_from = parse_iso_date(
        date_from_text
    )

    date_to = parse_iso_date(
        date_to_text
    )

    if date_from:
        queryset = queryset.filter(
            payment_date__gte=date_from
        )

    if date_to:
        queryset = queryset.filter(
            payment_date__lte=date_to
        )

    transactions = [
        decorate_transaction(transaction)
        for transaction in queryset
    ]

    posted_transactions = [
        transaction
        for transaction in transactions
        if (
            transaction.status
            == PaymentTransaction
            .Status
            .POSTED
        )
    ]

    receipt_total = sum(
        (
            Decimal(transaction.amount)
            for transaction
            in posted_transactions
            if (
                transaction.account.direction
                == SettlementAccount
                .Direction
                .RECEIVABLE
            )
        ),
        ZERO_MONEY,
    )

    payment_total = sum(
        (
            Decimal(transaction.amount)
            for transaction
            in posted_transactions
            if (
                transaction.account.direction
                == SettlementAccount
                .Direction
                .PAYABLE
            )
        ),
        ZERO_MONEY,
    )

    paginator = Paginator(
        transactions,
        30,
    )

    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    query_params = request.GET.copy()
    query_params.pop("page", None)

    return {
        "page_obj": page_obj,
        "query_without_page": (
            query_params.urlencode()
        ),
        "query": query,
        "direction_filter": (
            direction_filter
        ),
        "status_filter": status_filter,
        "method_filter": method_filter,
        "date_from": date_from_text,
        "date_to": date_to_text,
        "direction_choices": (
            SettlementAccount
            .Direction
            .choices
        ),
        "status_choices": (
            PaymentTransaction
            .Status
            .choices
        ),
        "method_choices": (
            PaymentTransaction
            .Method
            .choices
        ),
        "transaction_count": len(
            transactions
        ),
        "posted_count": len(
            posted_transactions
        ),
        "reversed_count": (
            len(transactions)
            - len(posted_transactions)
        ),
        "receipt_total": receipt_total,
        "payment_total": payment_total,
    }
