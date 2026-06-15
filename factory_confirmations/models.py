from django.conf import settings
from django.db import models

from orders.models import Order
from products.models import Product
from factories.models import Factory

class FactoryConfirmation(models.Model):
    """
    工厂确认文件。

    一个医院订单可能对应一个或多个工厂确认文件。
    第一版通常是一对一，但后续缺货补发时可能一对多。
    """

    class ExtractionStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    class FactoryMatchStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        OK = "ok", "OK"
        NEEDS_REVIEW = "needs_review", "Needs review"
        MANUALLY_CONFIRMED = "manually_confirmed", "Manually confirmed"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="factory_confirmations",
    )

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="factory_confirmations",
        help_text="匹配到的工厂库记录。",
    )

    factory_match_status = models.CharField(
        max_length=50,
        choices=FactoryMatchStatus.choices,
        default=FactoryMatchStatus.NOT_CHECKED,
        help_text="工厂匹配状态。",
    )

    factory_match_message = models.TextField(
        blank=True,
        help_text="工厂匹配说明。如果需要人工确认，会写在这里。",
    )
    
    confirmation_pdf = models.FileField(
        upload_to="factory_confirmations/%Y/%m/",
        help_text="工厂发来的含 serial number 和有效期的确认文件。",
    )

    extraction_status = models.CharField(
        max_length=30,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.NOT_STARTED,
    )

    extracted_confirmation_data = models.JSONField(
        null=True,
        blank=True,
        help_text="工厂确认文件提取后的结构化 JSON 数据。",
    )

    extraction_error = models.TextField(
        blank=True,
    )

    extracted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    shipping_date = models.DateField(
        null=True,
        blank=True,
        help_text="工厂文件中的 shipping date。",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_factory_confirmations",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    bon_de_commande_manual_confirmed = models.BooleanField(
        default=False,
        verbose_name="Bon de commande manually confirmed",
        help_text="Use this when the factory confirmation has no bon de commande or a mismatched bon de commande, but a human confirmed it belongs to the linked order.",
    )

    bon_de_commande_manual_note = models.TextField(
        blank=True,
        default="",
        verbose_name="Manual confirmation note",
    )

    bon_de_commande_manual_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Manually confirmed by",
    )

    bon_de_commande_manual_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Manually confirmed at",
    )

    notes = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Factory confirmation for {self.order.bon_de_commande}"


class SerialItem(models.Model):
    """
    工厂确认的具体 serial number。

    以后 PO 折扣规则会根据 expiration_date 判断：
    如果 expiration_date < document_date + 365 days，则 discount = 30%。
    """

    factory_confirmation = models.ForeignKey(
        FactoryConfirmation,
        on_delete=models.CASCADE,
        related_name="serial_items",
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="serial_items",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="serial_items",
    )

    product_code = models.CharField(
        max_length=100,
        db_index=True,
    )

    serial_number = models.CharField(
        max_length=255,
        db_index=True,
    )

    expiration_date = models.DateField(
        null=True,
        blank=True,
    )

    discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="折扣率，例如 0.30 表示 30%。",
    )

    raw_data = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["product_code", "serial_number"]
        unique_together = [
            ("order", "serial_number"),
        ]

    def __str__(self):
        return f"{self.product_code} - {self.serial_number}"