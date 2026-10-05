from django.utils.translation import gettext as _
from django.contrib import messages
from django.contrib.admin.views.decorators import (
    staff_member_required,
)
from django.core.exceptions import ValidationError
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.urls import reverse
from django.views.decorators.http import require_POST

from portal.services.settlement_portal_service import (
    decorate_account,
    decorate_transaction,
    get_account_queryset,
)
from settlements.forms import (
    PaymentEntryForm,
    PaymentReversalForm,
    SettlementDueDateForm,
)
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)
from settlements.services.settlement_action_service import (
    record_payment_transaction,
    reverse_payment_transaction,
    update_settlement_due_date,
)


def validation_error_text(error):
    if hasattr(error, "message_dict"):
        messages_list = []

        for field_messages in (
            error.message_dict.values()
        ):
            messages_list.extend(
                str(message)
                for message in field_messages
            )

        return "；".join(messages_list)

    if hasattr(error, "messages"):
        return "；".join(
            str(message)
            for message in error.messages
        )

    return str(error)


def get_decorated_account(account_id):
    account = get_object_or_404(
        get_account_queryset(),
        pk=account_id,
    )

    return decorate_account(account)


def build_account_detail_context(
    account,
    *,
    payment_form=None,
    due_date_form=None,
):
    transactions = (
        PaymentTransaction.objects
        .filter(account=account)
        .select_related(
            "created_by",
            "reversed_by",
        )
        .order_by(
            "-payment_date",
            "-id",
        )
    )

    decorated_transactions = [
        decorate_transaction(transaction)
        for transaction in transactions
    ]

    is_receivable = (
        account.direction
        == SettlementAccount
        .Direction
        .RECEIVABLE
    )

    if payment_form is None:
        payment_form = PaymentEntryForm(
            account=account
        )

    if due_date_form is None:
        due_date_form = (
            SettlementDueDateForm(
                account=account
            )
        )

    can_record_payment = (
        account.portal_remaining_amount > 0
        and account.portal_status
        != SettlementAccount
        .Status
        .CANCELLED
    )

    return {
        "account": account,
        "transactions": (
            decorated_transactions
        ),
        "payment_form": payment_form,
        "due_date_form": due_date_form,
        "is_receivable": is_receivable,
        "payment_noun": (
            _("收款")
            if is_receivable
            else _("付款")
        ),
        "payment_action_label": (
            _("登记收款")
            if is_receivable
            else _("登记付款")
        ),
        "posted_label": (
            _("已收金额")
            if is_receivable
            else _("已付金额")
        ),
        "remaining_label": (
            _("待收余额")
            if is_receivable
            else _("待付余额")
        ),
        "counterparty_label": (
            _("医院")
            if is_receivable
            else _("工厂")
        ),
        "can_record_payment": (
            can_record_payment
        ),
    }


def render_account_detail(
    request,
    account,
    *,
    payment_form=None,
    due_date_form=None,
    status=200,
):
    context = build_account_detail_context(
        account,
        payment_form=payment_form,
        due_date_form=due_date_form,
    )

    return render(
        request,
        "portal/settlements/account_detail.html",
        context,
        status=status,
    )


@staff_member_required
def settlement_account_detail(
    request,
    account_id,
):
    account = get_decorated_account(
        account_id
    )

    return render_account_detail(
        request,
        account,
    )


@staff_member_required
@require_POST
def settlement_record_payment(
    request,
    account_id,
):
    account = get_decorated_account(
        account_id
    )

    form = PaymentEntryForm(
        request.POST,
        account=account,
    )

    if not form.is_valid():
        return render_account_detail(
            request,
            account,
            payment_form=form,
            status=400,
        )

    try:
        payment = record_payment_transaction(
            account_id=account.id,
            payment_date=(
                form.cleaned_data[
                    "payment_date"
                ]
            ),
            amount=(
                form.cleaned_data["amount"]
            ),
            method=(
                form.cleaned_data["method"]
            ),
            reference=(
                form.cleaned_data["reference"]
            ),
            notes=(
                form.cleaned_data["notes"]
            ),
            user=request.user,
        )

    except ValidationError as exc:
        form.add_error(
            None,
            validation_error_text(exc),
        )

        account = get_decorated_account(
            account_id
        )

        return render_account_detail(
            request,
            account,
            payment_form=form,
            status=400,
        )

    action_name = (
        _("收款")
        if account.direction
        == SettlementAccount
        .Direction
        .RECEIVABLE
        else _("付款")
    )

    messages.success(
        request,
        (
            _('%(value1)s登记成功：%(value2)s %(value3)s') % {'value1': action_name, 'value2': payment.amount, 'value3': account.currency}
        ),
    )

    return redirect(
        reverse(
            "portal:settlement_account_detail",
            args=[account.id],
        )
    )


@staff_member_required
@require_POST
def settlement_update_due_date(
    request,
    account_id,
):
    account = get_decorated_account(
        account_id
    )

    form = SettlementDueDateForm(
        request.POST,
        account=account,
    )

    if not form.is_valid():
        return render_account_detail(
            request,
            account,
            due_date_form=form,
            status=400,
        )

    try:
        updated_account = (
            update_settlement_due_date(
                account_id=account.id,
                due_date=(
                    form.cleaned_data[
                        "due_date"
                    ]
                ),
            )
        )

    except ValidationError as exc:
        form.add_error(
            None,
            validation_error_text(exc),
        )

        account = get_decorated_account(
            account_id
        )

        return render_account_detail(
            request,
            account,
            due_date_form=form,
            status=400,
        )

    if updated_account.due_date:
        message = (
            _('付款截止日期已更新为 %(value1)s。') % {'value1': f'{updated_account.due_date:%Y-%m-%d}'}
        )
    else:
        message = (
            _("付款截止日期已清除。")
        )

    messages.success(
        request,
        message,
    )

    return redirect(
        reverse(
            "portal:settlement_account_detail",
            args=[account.id],
        )
    )


@staff_member_required
@require_POST
def settlement_reverse_transaction(
    request,
    transaction_id,
):
    payment = get_object_or_404(
        PaymentTransaction.objects
        .select_related("account"),
        pk=transaction_id,
    )

    account_id = payment.account_id

    form = PaymentReversalForm(
        request.POST
    )

    if not form.is_valid():
        messages.error(
            request,
            _("冲销时必须填写冲销原因。"),
        )

        return redirect(
            reverse(
                "portal:settlement_account_detail",
                args=[account_id],
            )
        )

    try:
        reverse_payment_transaction(
            transaction_id=payment.id,
            user=request.user,
            reason=(
                form.cleaned_data[
                    "reversal_reason"
                ]
            ),
        )

    except ValidationError as exc:
        messages.error(
            request,
            validation_error_text(exc),
        )

    else:
        messages.success(
            request,
            _("收付款流水已冲销。"),
        )

    return redirect(
        reverse(
            "portal:settlement_account_detail",
            args=[account_id],
        )
    )
