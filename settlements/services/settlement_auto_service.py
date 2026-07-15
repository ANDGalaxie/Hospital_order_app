from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from documents.models import GeneratedDocument
from settlements.models import SettlementAccount
from settlements.services.settlement_sync_service import (
    build_settlement_account_data,
)


ZERO_MONEY = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


SUPPORTED_DOCUMENT_TYPES = {
    GeneratedDocument
    .DocumentType
    .HOSPITAL_INVOICE,

    GeneratedDocument
    .DocumentType
    .FACTORY_PO,
}


AUTO_UPDATE_FIELDS = [
    "direction",
    "counterparty_name",
    "issue_date",
    "due_date",
    "currency",
    "original_amount",
]


PROTECTED_AFTER_PAYMENT_FIELDS = [
    "direction",
    "issue_date",
    "currency",
    "original_amount",
]


FIELD_LABELS = {
    "direction": "结算方向",
    "counterparty_name": "医院或工厂",
    "issue_date": "开立日期",
    "due_date": "付款截止日期",
    "currency": "币种",
    "original_amount": "结算金额",
}


def normalize_comparison_value(
    field_name,
    value,
):
    if field_name == "original_amount":
        return Decimal(value).quantize(
            MONEY_QUANT
        )

    return value


def values_are_equal(
    field_name,
    current_value,
    incoming_value,
):
    return (
        normalize_comparison_value(
            field_name,
            current_value,
        )
        ==
        normalize_comparison_value(
            field_name,
            incoming_value,
        )
    )


def find_changed_fields(
    account,
    account_data,
    field_names,
):
    changed_fields = []

    for field_name in field_names:
        current_value = getattr(
            account,
            field_name,
        )

        incoming_value = account_data[
            field_name
        ]

        if not values_are_equal(
            field_name,
            current_value,
            incoming_value,
        ):
            changed_fields.append(
                field_name
            )

    return changed_fields


@transaction.atomic
def ensure_settlement_account_for_document(
    document,
):
    """
    为正式 Invoice 或 Factory PO
    创建或校验结算账户。

    返回状态：
    - created
    - updated
    - unchanged
    - locked
    - unsupported
    """
    document = (
        GeneratedDocument.objects
        .select_for_update()
        .get(pk=document.pk)
    )

    if (
        document.document_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        return {
            "status": "unsupported",
            "account": None,
            "updated_fields": [],
        }

    account_data = (
        build_settlement_account_data(
            document
        )
    )

    account = (
        SettlementAccount.objects
        .select_for_update()
        .filter(document=document)
        .first()
    )

    if account is None:
        account = (
            SettlementAccount.objects
            .create(
                document=document,
                **account_data,
            )
        )

        return {
            "status": "created",
            "account": account,
            "updated_fields": list(
                AUTO_UPDATE_FIELDS
            ),
        }

    if (
        account.status
        == SettlementAccount
        .Status
        .CANCELLED
    ):
        raise ValidationError(
            "该正式文档对应的结算账户"
            "已经取消，不能使用相同文档编号"
            "重新生成。"
        )

    posted_amount = Decimal(
        account.posted_amount
    ).quantize(
        MONEY_QUANT
    )

    if posted_amount > ZERO_MONEY:
        protected_changes = (
            find_changed_fields(
                account,
                account_data,
                PROTECTED_AFTER_PAYMENT_FIELDS,
            )
        )

        if protected_changes:
            labels = "、".join(
                FIELD_LABELS.get(
                    field_name,
                    field_name,
                )
                for field_name
                in protected_changes
            )

            raise ValidationError(
                "该结算账户已经存在有效收付款，"
                f"不能修改：{labels}。"
                "请先核对正式文档和银行流水。"
            )

        # 已存在有效收付款时，不再自动覆盖
        # 对方名称、截止日期或其他快照数据。
        return {
            "status": "locked",
            "account": account,
            "updated_fields": [],
        }

    changed_fields = find_changed_fields(
        account,
        account_data,
        AUTO_UPDATE_FIELDS,
    )

    if not changed_fields:
        return {
            "status": "unchanged",
            "account": account,
            "updated_fields": [],
        }

    for field_name in changed_fields:
        setattr(
            account,
            field_name,
            account_data[field_name],
        )

    account.save(
        update_fields=[
            *changed_fields,
            "updated_at",
        ]
    )

    account.refresh_from_db()

    return {
        "status": "updated",
        "account": account,
        "updated_fields": changed_fields,
    }
