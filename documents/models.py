from django.conf import settings
from django.db import models

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

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="generated_documents",
    )

    document_type = models.CharField(
        max_length=50,
        choices=DocumentType.choices,
    )

    document_number = models.CharField(
        max_length=100,
        db_index=True,
    )

    pdf_file = models.FileField(
        upload_to="generated_documents/pdf/%Y/%m/",
        blank=True,
    )

    html_file = models.FileField(
        upload_to="generated_documents/html/%Y/%m/",
        blank=True,
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