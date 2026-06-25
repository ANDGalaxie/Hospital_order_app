from django.conf import settings
from django.db import models
from django.utils import timezone

from factories.models import Factory
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
        INVENTORY_PRODUCTS = "inventory_products", "库存产品"

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

class InventoryBatch(models.Model):
    """
    工厂新生产 / 新提供的一批库存文件。

    注意：
    这不是某一个医院订单的 FactoryConfirmation。
    它是一个库存批次，可以后续分配给多个订单。
    """

    class ExtractionStatus(models.TextChoices):
        PENDING = "pending", "待提取"
        SUCCESS = "success", "提取成功"
        FAILED = "failed", "提取失败"

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_batches",
        verbose_name="工厂",
    )

    batch_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="库存批次名称",
        help_text="例如：SINOMED 2026-09 生产批次",
    )

    source_pdf = models.FileField(
        upload_to="inventory_batches/",
        null=True,
        blank=True,
        verbose_name="工厂 Serial Number 文件",
    )

    batch_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="库存批次日期",
        help_text="可以理解为工厂发货日期或生产批次日期。",
    )

    extraction_status = models.CharField(
        max_length=20,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
        verbose_name="提取状态",
    )

    extracted_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name="提取结果",
    )

    extraction_error = models.TextField(
        blank=True,
        default="",
        verbose_name="提取错误",
    )

    notes = models.TextField(
        blank=True,
        default="",
        verbose_name="备注",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_inventory_batches",
        verbose_name="创建人",
    )

    extracted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-batch_date", "-created_at"]
        verbose_name = "库存批次"
        verbose_name_plural = "库存批次"

    def __str__(self):
        if self.batch_name:
            return self.batch_name
        if self.batch_date:
            return f"库存批次 {self.batch_date}"
        return f"库存批次 {self.id}"


class InventoryItem(models.Model):
    """
    库存里的每一个 Serial Number。
    """

    class Status(models.TextChoices):
        AVAILABLE = "available", "可用"
        RESERVED = "reserved", "已预留"
        ALLOCATED = "allocated", "已分配"
        CANCELLED = "cancelled", "作废"

    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="库存批次",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="产品",
    )

    product_code = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="产品号",
    )

    serial_number = models.CharField(
        max_length=200,
        unique=True,
        db_index=True,
        verbose_name="Serial Number",
        help_text="Serial Number 全局唯一，防止重复入库或重复发货。",
    )

    expiration_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="有效期",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
        verbose_name="库存状态",
    )

    allocated_order = models.ForeignKey(
        Order,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="allocated_inventory_items",
        verbose_name="已分配订单",
    )

    allocated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="分配时间",
    )

    raw_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name="原始提取数据",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product_code", "expiration_date", "serial_number"]
        verbose_name = "库存 Serial"
        verbose_name_plural = "库存 Serials"

    def __str__(self):
        return f"{self.product_code} / {self.serial_number}"


class InventoryProductFolder(models.Model):
    """
    库存产品虚拟文件夹。

    一行代表一个产品号的库存汇总。
    """

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="产品",
    )

    product_code = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name="产品号",
    )

    total_quantity = models.PositiveIntegerField(default=0)
    available_quantity = models.PositiveIntegerField(default=0)
    reserved_quantity = models.PositiveIntegerField(default=0)
    allocated_quantity = models.PositiveIntegerField(default=0)
    cancelled_quantity = models.PositiveIntegerField(default=0)

    earliest_expiration_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="最早有效期",
    )

    last_calculated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="最后计算时间",
    )

    class Meta:
        ordering = ["product_code"]
        verbose_name = "库存产品"
        verbose_name_plural = "库存产品"

    def __str__(self):
        return f"{self.product_code} / 可用 {self.available_quantity}"