from typing import Any, Dict, Optional, Tuple

from django.db import transaction
from django.db.models import Q

from pricing.services.price_policy_service import (
    apply_price_policy_to_order,
)


def get_stored_price_policy_result(order) -> Dict[str, Any]:
    """
    从订单提取 JSON 中读取最后一次价格规则应用结果。
    """
    data = order.extracted_order_data or {}

    if not isinstance(data, dict):
        return {}

    django_data = data.get("django") or {}

    if not isinstance(django_data, dict):
        return {}

    result = django_data.get("price_policy_result") or {}

    return result if isinstance(result, dict) else {}


def has_generated_financial_documents(order) -> bool:
    """
    Invoice 或 PO 已经生成后，暂时禁止普通重新计价。

    后续需要专门的财务修订流程，而不能直接覆盖价格快照。
    """
    try:
        from workflow.models import DocumentWorkflowItem

        return (
            DocumentWorkflowItem.objects.filter(
                order=order,
            )
            .filter(
                Q(invoice_document__isnull=False)
                | Q(po_document__isnull=False)
            )
            .exists()
        )

    except Exception:
        # 某些测试或旧环境中 workflow 尚不可用时，
        # 至少依赖 Order.status 判断。
        return False


def can_reapply_order_price_policy(
    order,
) -> Tuple[bool, str]:
    """
    判断是否允许重新应用价格规则。
    """
    documents_generated_status = getattr(
        getattr(order, "Status", None),
        "DOCUMENTS_GENERATED",
        "documents_generated",
    )

    if order.status == documents_generated_status:
        return (
            False,
            "该订单已经生成正式文件，不能直接覆盖价格快照。",
        )

    if has_generated_financial_documents(order):
        return (
            False,
            "该订单已经有关联的 Invoice 或 PO，"
            "不能直接覆盖价格快照。",
        )

    if not order.items.exists():
        return (
            False,
            "订单没有产品明细，无法应用价格规则。",
        )

    return True, ""


@transaction.atomic
def apply_and_store_order_price_policy(
    order,
    extracted_data: Optional[Dict[str, Any]] = None,
    *,
    save_order: bool = True,
) -> Dict[str, Any]:
    """
    应用价格规则，并把匹配结果保存到订单提取 JSON。

    extracted_data 可以是刚完成 OCR、尚未保存到数据库的新数据。
    这能避免价格引擎读到旧日期或空日期。
    """
    if extracted_data is None:
        extracted_data = order.extracted_order_data or {}

    if not isinstance(extracted_data, dict):
        extracted_data = {}

    # 关键：先把新 OCR 数据放入内存中的 order，
    # 价格引擎才能读取本次医院订单日期。
    order.extracted_order_data = extracted_data

    result = apply_price_policy_to_order(
        order=order,
        save=True,
    )

    django_data = extracted_data.setdefault(
        "django",
        {},
    )

    if not isinstance(django_data, dict):
        django_data = {}
        extracted_data["django"] = django_data

    django_data["price_policy_result"] = result

    order.extracted_order_data = extracted_data

    # 价格变动后，原有文件校验结果失效。
    order.document_validation_status = (
        order.DocumentValidationStatus.NOT_CHECKED
    )
    order.document_validation_data = None
    order.validated_at = None

    if save_order:
        order.save(
            update_fields=[
                "extracted_order_data",
                "document_validation_status",
                "document_validation_data",
                "validated_at",
                "updated_at",
            ]
        )

    return result


@transaction.atomic
def reapply_order_price_policy(
    order,
) -> Dict[str, Any]:
    """
    对现有订单重新应用当前有效价格规则。
    """
    allowed, reason = can_reapply_order_price_policy(
        order
    )

    if not allowed:
        raise ValueError(reason)

    return apply_and_store_order_price_policy(
        order=order,
        extracted_data=order.extracted_order_data,
        save_order=True,
    )
