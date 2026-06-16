from django.db import models
from django.utils import timezone

from orders.models import Order
from products.models import Product
from factory_confirmations.models import FactoryConfirmation


class ShipmentMonth(models.Model):
    """
    虚拟月份文件夹。
    例如：
        2026-06
    """

    month_key = models.CharField(
        max_length=7,
        unique=True,
        db_index=True,
        help_text="YYYY-MM",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month_key"]
        verbose_name = "Shipment Month"
        verbose_name_plural = "Shipment Batches"

    def __str__(self):
        return self.month_key


class ShipmentOrderFolder(models.Model):
    """
    虚拟订单文件夹。
    例如：
        2026-06 / Order 150222
    """

    month = models.ForeignKey(
        ShipmentMonth,
        on_delete=models.CASCADE,
        related_name="order_folders",
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="shipment_order_folders",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month__month_key", "order__bon_de_commande"]
        unique_together = ("month", "order")
        verbose_name = "Shipment Order Folder"
        verbose_name_plural = "Shipment Order Folders"

    def __str__(self):
        return f"{self.month.month_key} / Order {self.order.bon_de_commande}"


class ShipmentBatch(models.Model):
    """
    一个工厂确认文件对应一个发货批次。
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PARTIAL = "partial", "Partial"
        COMPLETE = "complete", "Complete"
        OVER_SHIPPED = "over_shipped", "Over shipped"
        NEEDS_REVIEW = "needs_review", "Needs review"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="shipment_batches",
    )

    order_folder = models.ForeignKey(
        ShipmentOrderFolder,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="batches",
    )

    factory_confirmation = models.OneToOneField(
        FactoryConfirmation,
        on_delete=models.CASCADE,
        related_name="shipment_batch",
    )

    batch_number = models.PositiveIntegerField(default=1)

    batch_date = models.DateField(
        null=True,
        blank=True,
        help_text="Usually factory confirmation shipping_date.",
    )

    month_key = models.CharField(
        max_length=7,
        db_index=True,
        help_text="YYYY-MM, used as virtual month folder.",
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.NEEDS_REVIEW,
    )

    total_requested_quantity = models.PositiveIntegerField(default=0)
    shipped_this_batch_quantity = models.PositiveIntegerField(default=0)
    total_shipped_after_batch_quantity = models.PositiveIntegerField(default=0)
    remaining_after_batch_quantity = models.PositiveIntegerField(default=0)

    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-batch_date", "-id"]
        verbose_name = "Shipment Batch"
        verbose_name_plural = "Shipment Batches"

    def __str__(self):
        return f"{self.month_key} / Order {self.order.bon_de_commande} / Batch {self.batch_number}"


class ShipmentBatchItem(models.Model):
    """
    本批已发产品。
    """

    batch = models.ForeignKey(
        ShipmentBatch,
        on_delete=models.CASCADE,
        related_name="shipped_items",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    product_code = models.CharField(max_length=100)
    shipped_quantity = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product_code"]
        verbose_name = "Shipped Item"
        verbose_name_plural = "Shipped Items"

    def __str__(self):
        return f"{self.product_code} shipped {self.shipped_quantity}"


class BackorderSnapshotItem(models.Model):
    """
    本批发货之后，每个产品的待发快照。
    """

    batch = models.ForeignKey(
        ShipmentBatch,
        on_delete=models.CASCADE,
        related_name="backorder_items",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    product_code = models.CharField(max_length=100)

    requested_quantity = models.PositiveIntegerField(default=0)
    shipped_before_batch_quantity = models.PositiveIntegerField(default=0)
    shipped_this_batch_quantity = models.PositiveIntegerField(default=0)
    total_shipped_after_batch_quantity = models.PositiveIntegerField(default=0)
    remaining_quantity = models.PositiveIntegerField(default=0)

    is_over_shipped = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product_code"]
        verbose_name = "Backorder Snapshot Item"
        verbose_name_plural = "Backorder Snapshot Items"

    def __str__(self):
        return f"{self.product_code} remaining {self.remaining_quantity}"