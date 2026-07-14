from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
)
from django.db import transaction
from django.db.models.deletion import (
    ProtectedError,
)

from documents.models import GeneratedDocument
from orders.models import Order
from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


def preview_test_order_purge(order):
    """
    返回 Admin 确认页需要显示的关联数据数量。
    """
    documents = (
        GeneratedDocument.objects
        .filter(order=order)
    )

    accounts = (
        SettlementAccount.objects
        .filter(
            document__order=order
        )
    )

    transactions = (
        PaymentTransaction.objects
        .filter(
            account__document__order=order
        )
    )

    return {
        "order_items": order.items.count(),
        "documents": documents.count(),
        "settlement_accounts": (
            accounts.count()
        ),
        "payment_transactions": (
            transactions.count()
        ),
    }


def protected_object_labels(error):
    labels = {
        str(
            obj._meta.verbose_name
        )
        for obj in error.protected_objects
    }

    return sorted(labels)


@transaction.atomic
def purge_test_order(
    *,
    order_id,
    requested_by,
):
    """
    永久删除一个测试订单。

    这是超级管理员专用通道，会绕过
    SettlementAccount 和 PaymentTransaction
    的常规删除保护。

    如果订单仍被其他 PROTECT 关系阻止，
    整个事务自动回滚。
    """
    if (
        requested_by is None
        or not requested_by.is_superuser
    ):
        raise PermissionDenied(
            "只有超级管理员可以清理测试订单。"
        )

    order = (
        Order.objects
        .select_for_update()
        .get(pk=order_id)
    )

    order_number = str(
        order.bon_de_commande
    )

    preview = preview_test_order_purge(
        order
    )

    # 必须先删除流水，因为流水通过 PROTECT
    # 关联 SettlementAccount。
    PaymentTransaction.objects.filter(
        account__document__order=order
    ).delete()

    # QuerySet.delete() 有意绕过模型的
    # SettlementAccount.delete() 保护。
    SettlementAccount.objects.filter(
        document__order=order
    ).delete()

    try:
        delete_result = (
            Order.objects
            .filter(pk=order.pk)
            .delete()
        )

    except ProtectedError as exc:
        labels = protected_object_labels(
            exc
        )

        protected_text = (
            "、".join(labels)
            if labels
            else "未知关联对象"
        )

        raise ValidationError(
            "订单仍然被以下对象保护，"
            f"本次删除已全部回滚："
            f"{protected_text}"
        ) from exc

    return {
        "order_number": order_number,
        "preview": preview,
        "deleted_total": (
            delete_result[0]
        ),
        "deleted_by_model": (
            delete_result[1]
        ),
    }
