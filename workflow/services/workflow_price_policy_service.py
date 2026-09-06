from django.db import transaction

from orders.services.order_price_policy_service import (
    reapply_order_price_policy,
)
from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_validation_service import (
    validate_document_workflow_item,
)


def is_workflow_item_fully_generated(item):
    """
    判断当前工作流的 Invoice 和 PO 是否都已生成。
    """
    return (
        item.invoice_status
        == DocumentWorkflowItem.DocumentStatus.GENERATED
        and item.po_status
        == DocumentWorkflowItem.DocumentStatus.GENERATED
    )


def workflow_has_any_generated_documents(item):
    """
    只要 Invoice 或 PO 任意一个已经生成，
    就不能直接覆盖订单价格快照。
    """
    return bool(
        item.invoice_document_id
        or item.po_document_id
        or (
            item.invoice_status
            == DocumentWorkflowItem.DocumentStatus.GENERATED
        )
        or (
            item.po_status
            == DocumentWorkflowItem.DocumentStatus.GENERATED
        )
    )


def reset_workflow_items_after_price_policy(order):
    """
    价格规则被重新应用后，OrderItem 的价格快照发生变化。

    同一个订单下尚未生成正式文件的 WorkflowItem，
    都需要回到“未验证”状态。

    已经生成任意正式文件的 WorkflowItem 不做修改。
    """
    reset_count = 0
    skipped_generated_count = 0

    workflow_items = (
        DocumentWorkflowItem.objects
        .filter(order=order)
        .select_related(
            "invoice_document",
            "po_document",
        )
    )

    for workflow_item in workflow_items:
        if workflow_has_any_generated_documents(
            workflow_item
        ):
            skipped_generated_count += 1
            continue

        workflow_item.validation_status = (
            DocumentWorkflowItem
            .ValidationStatus
            .NOT_VALIDATED
        )

        workflow_item.validation_data = None
        workflow_item.validated_at = None

        if (
            workflow_item.workflow_status
            != DocumentWorkflowItem
            .WorkflowStatus
            .GENERATED
        ):
            workflow_item.workflow_status = (
                DocumentWorkflowItem
                .WorkflowStatus
                .PENDING
            )

        workflow_item.save(
            update_fields=[
                "validation_status",
                "validation_data",
                "validated_at",
                "workflow_status",
                "updated_at",
            ]
        )

        reset_count += 1

    return {
        "reset_count": reset_count,
        "skipped_generated_count": (
            skipped_generated_count
        ),
    }


def apply_price_policy_to_workflow_items(
    queryset,
):
    """
    保留给 Django Admin 使用的批量重新计价接口。

    规则：
    - 价格规则实际作用于 Order；
    - 同一个 Order 只重新应用一次；
    - 重新应用后，重置相关 WorkflowItem 的验证状态；
    - 已生成正式文件的订单会被安全拒绝。
    """
    selected_items = list(
        queryset.select_related(
            "order",
            "shipment_batch",
            "invoice_document",
            "po_document",
        )
    )

    order_map = {}

    for item in selected_items:
        if (
            item.order_id
            and item.order_id not in order_map
        ):
            order_map[item.order_id] = item.order

    summary = {
        "selected_items": len(selected_items),
        "order_count": len(order_map),
        "success_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "reset_workflow_item_count": 0,
        "skipped_generated_item_count": 0,
        "results": [],
    }

    for order in order_map.values():
        try:
            price_result = (
                reapply_order_price_policy(
                    order
                )
            )

        except Exception as exc:
            summary["error_count"] += 1

            summary["results"].append(
                {
                    "order_id": order.id,
                    "bon_de_commande": (
                        order.bon_de_commande
                    ),
                    "success": False,
                    "errors": [str(exc)],
                    "warnings": [],
                    "updated_count": 0,
                    "price_policy_date": None,
                    "date_source": "",
                    "reset_count": 0,
                    "skipped_generated_count": 0,
                }
            )

            continue

        errors = (
            price_result.get("errors")
            or []
        )

        warnings = (
            price_result.get("warnings")
            or []
        )

        if errors:
            summary["error_count"] += 1

            summary["results"].append(
                {
                    "order_id": order.id,
                    "bon_de_commande": (
                        order.bon_de_commande
                    ),
                    "success": False,
                    "errors": errors,
                    "warnings": warnings,
                    "updated_count": (
                        price_result.get(
                            "updated_count",
                            0,
                        )
                    ),
                    "price_policy_date": (
                        price_result.get(
                            "price_policy_date"
                        )
                    ),
                    "date_source": (
                        price_result.get(
                            "date_source",
                            "",
                        )
                    ),
                    "reset_count": 0,
                    "skipped_generated_count": 0,
                }
            )

            continue

        reset_result = (
            reset_workflow_items_after_price_policy(
                order
            )
        )

        summary["success_count"] += 1
        summary["warning_count"] += len(
            warnings
        )

        summary[
            "reset_workflow_item_count"
        ] += reset_result["reset_count"]

        summary[
            "skipped_generated_item_count"
        ] += reset_result[
            "skipped_generated_count"
        ]

        summary["results"].append(
            {
                "order_id": order.id,
                "bon_de_commande": (
                    order.bon_de_commande
                ),
                "success": True,
                "errors": [],
                "warnings": warnings,
                "updated_count": (
                    price_result.get(
                        "updated_count",
                        0,
                    )
                ),
                "price_policy_date": (
                    price_result.get(
                        "price_policy_date"
                    )
                ),
                "date_source": (
                    price_result.get(
                        "date_source",
                        "",
                    )
                ),
                "reset_count": (
                    reset_result[
                        "reset_count"
                    ]
                ),
                "skipped_generated_count": (
                    reset_result[
                        "skipped_generated_count"
                    ]
                ),
            }
        )

    return summary


@transaction.atomic
def reapply_prices_and_validate_workflow_item(
    workflow_item,
):
    """
    Portal 工作流详情页使用。

    执行顺序：
    1. 锁定并重新读取 WorkflowItem；
    2. 确认尚未生成 Invoice 或 PO；
    3. 对关联订单重新应用 PricePolicy；
    4. 重置同一订单下尚未生成文件的 WorkflowItem；
    5. 对当前 WorkflowItem 立即重新验证；
    6. 返回价格匹配和验证结果。
    """
    item = (
        DocumentWorkflowItem.objects
        .select_for_update()
        .select_related(
            "order",
            "shipment_batch",
        )
        .get(id=workflow_item.id)
    )

    if not item.order_id:
        raise ValueError(
            "该工作流没有关联医院订单。"
        )

    if workflow_has_any_generated_documents(
        item
    ):
        raise ValueError(
            "该工作流已经生成 Invoice 或 PO，"
            "不能直接覆盖价格快照。"
        )

    price_result = (
        reapply_order_price_policy(
            item.order
        )
    )

    price_errors = (
        price_result.get("errors")
        or []
    )

    if price_errors:
        raise ValueError(
            "；".join(
                str(error)
                for error in price_errors
            )
        )

    reset_result = (
        reset_workflow_items_after_price_policy(
            item.order
        )
    )

    # reset 后，当前 item 的状态已经变化，
    # 必须重新从数据库读取。
    item.refresh_from_db()

    validation_result = (
        validate_document_workflow_item(
            item=item,
            save=True,
        )
    )

    item.refresh_from_db()

    return {
        "workflow_item_id": item.id,
        "order_id": item.order_id,
        "price_result": price_result,
        "reset_result": reset_result,
        "validation_result": validation_result,
        "validation_status": (
            item.validation_status
        ),
        "workflow_status": (
            item.workflow_status
        ),
    }
