from django.db import models


class DocumentWorkflowItem(models.Model):
    """
    文件处理中心的一条待处理任务。

    一条记录 = 一个 ShipmentBatch 对应的一组文件处理任务。
    ShipmentBatch 只负责发货历史；
    DocumentWorkflowItem 负责 validate / generate 的工作流状态。
    """

    class WorkflowStatus(models.TextChoices):
        PENDING = "pending", "待处理"
        READY_TO_GENERATE = "ready_to_generate", "可以生成"
        GENERATED = "generated", "已生成"
        BLOCKED = "blocked", "阻塞"
        CANCELLED = "cancelled", "取消"

    class ValidationStatus(models.TextChoices):
        NOT_VALIDATED = "not_validated", "未校验"
        READY = "ready", "可以生成"
        NEEDS_REVIEW = "needs_review", "需要人工检查"
        BLOCKED = "blocked", "禁止生成"

    class DocumentStatus(models.TextChoices):
        NOT_GENERATED = "not_generated", "未生成"
        GENERATED = "generated", "已生成"
        FAILED = "failed", "生成失败"

    shipment_batch = models.OneToOneField(
        "shipments.ShipmentBatch",
        on_delete=models.CASCADE,
        related_name="document_workflow_item",
        verbose_name="发货批次",
    )

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="document_workflow_items",
        verbose_name="订单",
    )

    workflow_status = models.CharField(
        max_length=30,
        choices=WorkflowStatus.choices,
        default=WorkflowStatus.PENDING,
        verbose_name="流程状态",
    )

    validation_status = models.CharField(
        max_length=30,
        choices=ValidationStatus.choices,
        default=ValidationStatus.NOT_VALIDATED,
        verbose_name="校验状态",
    )

    validation_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name="校验结果",
    )

    validated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="校验时间",
    )

    invoice_status = models.CharField(
        max_length=30,
        choices=DocumentStatus.choices,
        default=DocumentStatus.NOT_GENERATED,
        verbose_name="Invoice 状态",
    )

    po_status = models.CharField(
        max_length=30,
        choices=DocumentStatus.choices,
        default=DocumentStatus.NOT_GENERATED,
        verbose_name="Factory PO 状态",
    )

    invoice_document = models.ForeignKey(
        "documents.GeneratedDocument",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_workflow_items",
        verbose_name="Invoice 文件",
    )

    po_document = models.ForeignKey(
        "documents.GeneratedDocument",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="po_workflow_items",
        verbose_name="Factory PO 文件",
    )

    notes = models.TextField(
        blank=True,
        default="",
        verbose_name="备注",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "待处理文件"
        verbose_name_plural = "待处理文件"

    def __str__(self):
        return (
            f"Order {self.order.bon_de_commande} / "
            f"Batch {self.shipment_batch.batch_number}"
        )