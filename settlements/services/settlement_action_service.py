from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


ZERO_MONEY = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


def normalize_money(value):
    try:
        amount = Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as exc:
        raise ValidationError(
            "收付款金额格式无效。"
        ) from exc

    return amount.quantize(MONEY_QUANT)


@transaction.atomic
def record_payment_transaction(
    *,
    account_id,
    payment_date,
    amount,
    method,
    reference,
    notes,
    user,
):
    """
    登记一笔实际收款或付款。

    通过 select_for_update 锁定结算账户，
    避免两名用户同时登记而造成超额付款。
    """
    account = (
        SettlementAccount.objects
        .select_for_update()
        .select_related("document")
        .get(pk=account_id)
    )

    if (
        account.status
        == SettlementAccount.Status.CANCELLED
    ):
        raise ValidationError(
            "已取消的结算账户不能登记收付款。"
        )

    normalized_amount = normalize_money(
        amount
    )

    if normalized_amount <= ZERO_MONEY:
        raise ValidationError(
            "收付款金额必须大于 0。"
        )

    posted_amount = (
        PaymentTransaction.objects
        .filter(
            account=account,
            status=(
                PaymentTransaction.Status.POSTED
            ),
        )
        .aggregate(total=Sum("amount"))
        .get("total")
        or ZERO_MONEY
    )

    remaining_amount = (
        Decimal(account.original_amount)
        - Decimal(posted_amount)
    ).quantize(MONEY_QUANT)

    if remaining_amount <= ZERO_MONEY:
        raise ValidationError(
            "该结算账户已经结清。"
        )

    if normalized_amount > remaining_amount:
        raise ValidationError(
            "本次金额将导致累计收付款金额"
            "超过结算账户原始金额。"
        )

    payment = PaymentTransaction(
        account=account,
        payment_date=payment_date,
        amount=normalized_amount,
        method=method,
        reference=str(
            reference or ""
        ).strip(),
        notes=str(
            notes or ""
        ).strip(),
        created_by=user,
    )

    payment.save()

    account.refresh_status()

    return payment


@transaction.atomic
def update_settlement_due_date(
    *,
    account_id,
    due_date,
):
    account = (
        SettlementAccount.objects
        .select_for_update()
        .get(pk=account_id)
    )

    if (
        account.status
        == SettlementAccount.Status.CANCELLED
    ):
        raise ValidationError(
            "已取消的结算账户不能修改"
            "付款截止日期。"
        )

    if (
        due_date
        and due_date < account.issue_date
    ):
        raise ValidationError(
            "付款截止日期不能早于开立日期。"
        )

    account.due_date = due_date

    account.save(
        update_fields=[
            "due_date",
            "updated_at",
        ]
    )

    account.refresh_status()

    return account


@transaction.atomic
def reverse_payment_transaction(
    *,
    transaction_id,
    user,
    reason,
):
    payment = (
        PaymentTransaction.objects
        .select_for_update()
        .select_related("account")
        .get(pk=transaction_id)
    )

    if (
        payment.status
        == PaymentTransaction.Status.REVERSED
    ):
        raise ValidationError(
            "该流水已经冲销。"
        )

    payment.reverse(
        user=user,
        reason=reason,
    )

    return payment
