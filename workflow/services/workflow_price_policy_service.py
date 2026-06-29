from pricing.services.price_policy_service import apply_price_policy_to_order
from workflow.models import DocumentWorkflowItem


def is_workflow_item_fully_generated(item):
    return (
        item.invoice_status == DocumentWorkflowItem.DocumentStatus.GENERATED
        and item.po_status == DocumentWorkflowItem.DocumentStatus.GENERATED
    )


def reset_workflow_items_after_price_policy(order):
    """
    价格策略会改变 OrderItem 的价格快照。

    因此同一个 Order 下尚未生成文件的 WorkflowItem，
    都应该回到“未校验”状态，等待重新 validate。
    """
    reset_count = 0
    skipped_generated_count = 0

    items = DocumentWorkflowItem.objects.filter(order=order)

    for item in items:
        if is_workflow_item_fully_generated(item):
            skipped_generated_count += 1
            continue

        item.validation_status = DocumentWorkflowItem.ValidationStatus.NOT_VALIDATED
        item.validation_data = None
        item.validated_at = None

        if item.workflow_status != DocumentWorkflowItem.WorkflowStatus.GENERATED:
            item.workflow_status = DocumentWorkflowItem.WorkflowStatus.PENDING

        item.save(
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
        "skipped_generated_count": skipped_generated_count,
    }


def apply_price_policy_to_workflow_items(queryset):
    """
    对选中的 WorkflowItem 对应的 Order 执行价格策略。

    注意：
      价格策略作用对象是 Order，不是 ShipmentBatch。
      同一个 Order 只执行一次。
    """
    selected_items = list(
        queryset.select_related("order", "shipment_batch")
    )

    order_map = {}

    for item in selected_items:
        if item.order_id and item.order_id not in order_map:
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
        result = apply_price_policy_to_order(order)

        errors = result.get("errors", [])
        warnings = result.get("warnings", [])

        if errors:
            summary["error_count"] += 1
            summary["results"].append(
                {
                    "order_id": order.id,
                    "bon_de_commande": order.bon_de_commande,
                    "success": False,
                    "errors": errors,
                    "warnings": warnings,
                    "updated_count": result.get("updated_count", 0),
                    "price_policy_date": result.get("price_policy_date"),
                    "date_source": result.get("date_source"),
                    "reset_count": 0,
                    "skipped_generated_count": 0,
                }
            )
            continue

        reset_result = reset_workflow_items_after_price_policy(order)

        summary["success_count"] += 1
        summary["warning_count"] += len(warnings)
        summary["reset_workflow_item_count"] += reset_result["reset_count"]
        summary["skipped_generated_item_count"] += reset_result["skipped_generated_count"]

        summary["results"].append(
            {
                "order_id": order.id,
                "bon_de_commande": order.bon_de_commande,
                "success": True,
                "errors": [],
                "warnings": warnings,
                "updated_count": result.get("updated_count", 0),
                "price_policy_date": result.get("price_policy_date"),
                "date_source": result.get("date_source"),
                "reset_count": reset_result["reset_count"],
                "skipped_generated_count": reset_result["skipped_generated_count"],
            }
        )

    return summary
