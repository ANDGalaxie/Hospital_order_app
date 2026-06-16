from django.db import models
from django.utils import timezone

from orders.models import Order
from products.models import Product


class BackorderRootFolder(models.Model):
    """
    待发产品库根目录下的两个虚拟文件夹：
      1. 待发产品
      2. 预计可发产品
    """

    class FolderCode(models.TextChoices):
        BACKORDER_ORDERS = "backorder_orders", "待发产品"
        EXPECTED_SHIPPING = "expected_shipping", "预计可发产品"

    code = models.CharField(
        max_length=50,
        unique=True,
        choices=FolderCode.choices,
    )

    name = models.CharField(max_length=100)

    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Backorder Library"
        verbose_name_plural = "Backorder Library"

    def __str__(self):
        return self.name


class BackorderOrderFolder(models.Model):
    """
    当前仍有待发产品的订单文件夹。
    例如：
      Order 150222
    """

    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name="backorder_folder",
    )

    line_count = models.PositiveIntegerField(default=0)
    remaining_total_quantity = models.PositiveIntegerField(default=0)

    earliest_expected_shipping_date = models.DateField(
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
        help_text="True means this order still has remaining products.",
    )

    last_calculated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order__bon_de_commande"]
        verbose_name = "Backorder Order Folder"
        verbose_name_plural = "Backorder Orders"

    def __str__(self):
        return f"Order {self.order.bon_de_commande}"


class BackorderLine(models.Model):
    """
    当前待发产品行。
    一行代表：
      某个订单 + 某个产品号 当前还欠多少。
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PLANNED = "planned", "Planned"
        OVERDUE = "overdue", "Overdue"
        COMPLETED = "completed", "Completed"

    order_folder = models.ForeignKey(
        BackorderOrderFolder,
        on_delete=models.CASCADE,
        related_name="lines",
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="backorder_lines",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    product_code = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")

    requested_quantity = models.PositiveIntegerField(default=0)
    shipped_quantity = models.PositiveIntegerField(default=0)
    remaining_quantity = models.PositiveIntegerField(default=0)

    expected_shipping_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Expected shipping date",
        help_text="Manually entered expected shipping date.",
    )

    expected_shipping_note = models.TextField(
        blank=True,
        default="",
        verbose_name="Expected shipping note",
    )

    expected_month_key = models.CharField(
        max_length=20,
        default="no_date",
        db_index=True,
        help_text="YYYY-MM or no_date.",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )

    is_active = models.BooleanField(
        default=True,
        help_text="False when this line is fully shipped.",
    )

    last_calculated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order__bon_de_commande", "product_code"]
        unique_together = ("order", "product_code")
        verbose_name = "Backorder Line"
        verbose_name_plural = "Backorder Lines"

    def refresh_status_fields(self):
        if self.remaining_quantity <= 0:
            self.is_active = False
            self.status = self.Status.COMPLETED
        else:
            self.is_active = True

            if not self.expected_shipping_date:
                self.status = self.Status.OPEN
            elif self.expected_shipping_date < timezone.localdate():
                self.status = self.Status.OVERDUE
            else:
                self.status = self.Status.PLANNED

        if self.expected_shipping_date:
            self.expected_month_key = self.expected_shipping_date.strftime("%Y-%m")
        else:
            self.expected_month_key = "no_date"

    def save(self, *args, **kwargs):
        self.refresh_status_fields()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.order.bon_de_commande} / {self.product_code} / remaining {self.remaining_quantity}"


class ExpectedShippingMonthFolder(models.Model):
    """
    预计可发产品的月份文件夹。
    例如：
      2026-09
      No expected date
    """

    month_key = models.CharField(max_length=20, unique=True, db_index=True)
    display_name = models.CharField(max_length=100)

    line_count = models.PositiveIntegerField(default=0)
    product_count = models.PositiveIntegerField(default=0)
    total_remaining_quantity = models.PositiveIntegerField(default=0)

    sort_order = models.CharField(max_length=20, default="9999-99")

    last_calculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sort_order"]
        verbose_name = "Expected Shipping Month"
        verbose_name_plural = "Expected Shipping Months"

    def __str__(self):
        return self.display_name


class ExpectedShippingProductFolder(models.Model):
    """
    某个月下，按产品号汇总的预计可发产品。
    例如：
      2026-09 / BMA-2.5015 / total remaining 5
    """

    month_folder = models.ForeignKey(
        ExpectedShippingMonthFolder,
        on_delete=models.CASCADE,
        related_name="product_folders",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    product_code = models.CharField(max_length=100)

    order_count = models.PositiveIntegerField(default=0)
    line_count = models.PositiveIntegerField(default=0)
    total_remaining_quantity = models.PositiveIntegerField(default=0)

    last_calculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["month_folder__sort_order", "product_code"]
        unique_together = ("month_folder", "product_code")
        verbose_name = "Expected Shipping Product"
        verbose_name_plural = "Expected Shipping Products"

    def __str__(self):
        return f"{self.month_folder.display_name} / {self.product_code}"