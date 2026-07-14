from django.db import transaction
from django.utils import timezone

from workflow.models import DocumentWorkflowItem
from workflow.services.workflow_price_validation_service import (
    validate_workflow_price_snapshots,
)
from shipments.services.shipment_validation_service import validate_shipment_batch


def map_batch_validation_status_to_workflow_status(batch_status):
    """
    把 ShipmentBatch validation status 转成 WorkflowItem validation status。
    """
    if batch_status == "ready":
        return DocumentWorkflowItem.ValidationStatus.READY

    if batch_status == "blocked":
        return DocumentWorkflowItem.ValidationStatus.BLOCKED

    if batch_status == "needs_review":
        return DocumentWorkflowItem.ValidationStatus.NEEDS_REVIEW

    return DocumentWorkflowItem.ValidationStatus.NOT_VALIDATED


def decide_workflow_status(item, validation_status):
    """
    根据校验结果决定 workflow 状态。

    注意：
      如果 Invoice 和 PO 都已经生成，则保持 generated。
      否则：
        ready -> ready_to_generate
        blocked -> blocked
        needs_review -> pending
    """
    if (
        item.invoice_status == DocumentWorkflowItem.DocumentStatus.GENERATED
        and item.po_status == DocumentWorkflowItem.DocumentStatus.GENERATED
    ):
        return DocumentWorkflowItem.WorkflowStatus.GENERATED

    if validation_status == DocumentWorkflowItem.ValidationStatus.READY:
        return DocumentWorkflowItem.WorkflowStatus.READY_TO_GENERATE

    if validation_status == DocumentWorkflowItem.ValidationStatus.BLOCKED:
        return DocumentWorkflowItem.WorkflowStatus.BLOCKED

    return DocumentWorkflowItem.WorkflowStatus.PENDING


@transaction.atomic
def validate_document_workflow_item(
    item,
    save=True,
):
    """
    校验一个 DocumentWorkflowItem。

    验证包含：
    1. ShipmentBatch 来源、数量、Serial、有效期；
    2. OrderItem 正式价格快照；
    3. 综合决定是否允许生成文件。
    """
    item = (
        DocumentWorkflowItem.objects
        .select_for_update()
        .select_related(
            "shipment_batch",
            "order",
        )
        .get(id=item.id)
    )

    batch_result = validate_shipment_batch(
        batch=item.shipment_batch,
        save=False,
    )

    price_result = (
        validate_workflow_price_snapshots(
            workflow_item=item,
        )
    )

    errors = list(
        batch_result.get("errors")
        or []
    )

    errors.extend(
        price_result.get("errors")
        or []
    )

    warnings = list(
        batch_result.get("warnings")
        or []
    )

    warnings.extend(
        price_result.get("warnings")
        or []
    )

    if errors:
        validation_status = (
            DocumentWorkflowItem
            .ValidationStatus
            .BLOCKED
        )

    elif warnings:
        validation_status = (
            DocumentWorkflowItem
            .ValidationStatus
            .NEEDS_REVIEW
        )

    else:
        validation_status = (
            DocumentWorkflowItem
            .ValidationStatus
            .READY
        )

    workflow_status = decide_workflow_status(
        item=item,
        validation_status=validation_status,
    )

    result = dict(batch_result)

    result.update(
        {
            "can_generate_documents": (
                validation_status
                == DocumentWorkflowItem
                .ValidationStatus
                .READY
            ),
            "validation_status": (
                validation_status
            ),
            "errors": errors,
            "warnings": warnings,
            "price_validation": (
                price_result
            ),
            "checked_at": (
                timezone.now().isoformat()
            ),
        }
    )

    if save:
        item.validation_status = (
            validation_status
        )

        item.validation_data = result
        item.validated_at = timezone.now()
        item.workflow_status = workflow_status

        item.save(
            update_fields=[
                "validation_status",
                "validation_data",
                "validated_at",
                "workflow_status",
                "updated_at",
            ]
        )

    return result


def validate_document_workflow_items(queryset):
    """
    批量校验 WorkflowItem。
    """
    summary = {
        "processed": 0,
        "ready": 0,
        "needs_review": 0,
        "blocked": 0,
        "failed": 0,
        "results": [],
    }

    for item in queryset:
        try:
            result = validate_document_workflow_item(
                item=item,
                save=True,
            )

            summary["processed"] += 1

            status = result.get("validation_status")

            if status == "ready":
                summary["ready"] += 1
            elif status == "blocked":
                summary["blocked"] += 1
            else:
                summary["needs_review"] += 1

            summary["results"].append(
                {
                    "item_id": item.id,
                    "order": item.order.bon_de_commande,
                    "batch_number": item.shipment_batch.batch_number,
                    "status": status,
                    "error_count": len(result.get("errors", [])),
                    "warning_count": len(result.get("warnings", [])),
                }
            )

        except Exception as exc:
            summary["failed"] += 1
            summary["results"].append(
                {
                    "item_id": item.id,
                    "error": str(exc),
                }
            )

    return summary
