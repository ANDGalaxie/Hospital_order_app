from django.conf import settings
from django.db import models
from datetime import timedelta

from orders.models import Order


class DocumentSequence(models.Model):
    """
    统一编号表。

    替代原来的 document_registry.json。

    同一个 bon_de_commande 在同一个月份应该复用同一个 sequence。
    Invoice 和 PO 共享这个 sequence。
    """

    month_key = models.CharField(
        max_length=7,
        db_index=True,
        help_text="格式：YYYY-MM，例如 2026-06。",
    )

    bon_de_commande = models.CharField(
        max_length=100,
        db_index=True,
    )

    sequence = models.PositiveIntegerField()

    invoice_number = models.CharField(
        max_length=100,
        blank=True,
    )

    po_number = models.CharField(
        max_length=100,
        blank=True,
    )

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "未付款"
        PAID = "paid", "已付款"

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
        verbose_name="付款状态",
    )

    payment_due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="付款截止日期",
    )

    payment_reminder_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="提醒日期",
        help_text="自动计算：付款截止日期前 10 天。",
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="付款时间",
    )

    payment_notes = models.TextField(
        blank=True,
        default="",
        verbose_name="付款备注",
    )
    
    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        unique_together = [
            ("month_key", "bon_de_commande"),
            ("month_key", "sequence"),
        ]
        ordering = ["-month_key", "sequence"]

    def save(self, *args, **kwargs):
        if self.payment_due_date:
            self.payment_reminder_date = self.payment_due_date - timedelta(days=10)
        else:
            self.payment_reminder_date = None

        super().save(*args, **kwargs)
        
    def __str__(self):
        return f"{self.month_key} - {self.bon_de_commande} - {self.sequence}"


class GeneratedDocument(models.Model):
    """
    已生成文件记录。

    每次生成 Invoice 或 Factory PO，都要在这里保存记录。
    """

    class DocumentType(models.TextChoices):
        HOSPITAL_INVOICE = "hospital_invoice", "Hospital Invoice"
        FACTORY_PO = "factory_po", "Factory Purchase Order"
        FACTORY_ORDER_REQUEST = "factory_order_request", "Factory Order Request"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="generated_documents",
    )

    shipment_batch = models.ForeignKey(
        "shipments.ShipmentBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_documents",
        verbose_name="发货批次",
    )
    
    document_type = models.CharField(
        max_length=50,
        choices=DocumentType.choices,
    )

    document_number = models.CharField(
        max_length=200,
        db_index=True,
    )

    pdf_file = models.FileField(
        upload_to="generated_documents/pdf/%Y/%m/",
        blank=True,
        max_length=500,
    )

    html_file = models.FileField(
        upload_to="generated_documents/html/%Y/%m/",
        blank=True,
        max_length=500,
    )

    source_data = models.JSONField(
        null=True,
        blank=True,
        help_text="生成该文件时使用的数据快照。",
    )

    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="generated_documents",
    )

    generated_at = models.DateTimeField(
        auto_now_add=True,
    )

    notes = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ["-generated_at"]
        unique_together = [
            ("document_type", "document_number"),
        ]

    def __str__(self):
        return f"{self.document_number} - {self.document_type}"

class HospitalInvoiceDocument(GeneratedDocument):
    class Meta:
        proxy = True
        verbose_name = "Hospital invoice"
        verbose_name_plural = "Hospital invoices"


class FactoryPurchaseOrderDocument(GeneratedDocument):
    class Meta:
        proxy = True
        verbose_name = "Factory purchase order"
        verbose_name_plural = "Factory purchase orders"

class FactoryOrderRequestDocument(GeneratedDocument):
    class Meta:
        proxy = True
        verbose_name = "Factory Order Request"
        verbose_name_plural = "Factory Order Requests"