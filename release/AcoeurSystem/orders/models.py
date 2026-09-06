from django.conf import settings
from django.db import models
from hospitals.models import Hospital
from products.models import Product
from factories.models import Factory

class Order(models.Model):
    """
    医院订单主表。

    Milestone 1 阶段只保存最基础的信息：
    - bon_de_commande：医院订单号
    - hospital_name：医院名称，先手动输入
    - hospital_order_pdf：医院上传的订单 PDF
    - status：订单状态
    - created_by：创建订单的内部员工
    - created_at / updated_at：创建和更新时间

    后续 Milestone 2 会加入：
    - OCR 提取结果 JSON
    - 人工确认后的订单数据
    - 产品列表
    - 收货地址
    - 账单地址匹配结果
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        HOSPITAL_ORDER_UPLOADED = "hospital_order_uploaded", "Hospital order uploaded"
        EXTRACTED = "extracted", "Extracted"
        CONFIRMED = "confirmed", "Confirmed"
        DOCUMENTS_GENERATED = "documents_generated", "Documents generated"
        
    class ExtractionStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    class HospitalMatchStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        OK = "ok", "OK"
        NEEDS_REVIEW = "needs_review", "Needs review"
        MANUALLY_CONFIRMED = "manually_confirmed", "Manually confirmed"

    class FactoryMatchStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        OK = "ok", "OK"
        NEEDS_REVIEW = "needs_review", "Needs review"
        MANUALLY_CONFIRMED = "manually_confirmed", "Manually confirmed"

    class DocumentValidationStatus(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        READY = "ready", "Ready"
        NEEDS_REVIEW = "needs_review", "Needs review"
        BLOCKED = "blocked", "Blocked"
        
    bon_de_commande = models.CharField(
        max_length=100,
        unique=True,
        help_text="医院订单号，例如 150222",
    )

    hospital_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="医院名称。Milestone 1 可手动填写；后续由 OCR 自动提取。",
    )

    hospital_order_pdf = models.FileField(
        upload_to="hospital_orders/%Y/%m/",
        help_text="医院发来的订单 PDF。",
    )

    extraction_status = models.CharField(
        max_length=30,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.NOT_STARTED,
        help_text="医院订单 OCR / extraction 状态。",
    )

    extracted_order_data = models.JSONField(
        null=True,
        blank=True,
        help_text="医院订单 OCR 提取后的结构化 JSON 数据。",
    )

    hospital = models.ForeignKey(
        Hospital,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        help_text="匹配到的医院数据库记录。",
    )

    hospital_match_status = models.CharField(
        max_length=50,
        choices=HospitalMatchStatus.choices,
        default=HospitalMatchStatus.NOT_CHECKED,
        help_text="医院数据库匹配状态。",
    )

    hospital_match_message = models.TextField(
        blank=True,
        help_text="医院匹配说明。如果需要人工确认，会写在这里。",
    )

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        help_text="从医院订单中的 supplier / fournisseur 区域匹配到的工厂。",
    )

    factory_match_status = models.CharField(
        max_length=50,
        choices=FactoryMatchStatus.choices,
        default=FactoryMatchStatus.NOT_CHECKED,
        help_text="工厂数据库匹配状态。",
    )

    factory_match_message = models.TextField(
        blank=True,
        help_text="工厂匹配说明。如果需要人工确认，会写在这里。",
    )
    
    confirmed_order_data = models.JSONField(
        null=True,
        blank=True,
        help_text="人工确认后的订单数据。后续生成 Invoice / PO 应优先使用这个字段。",
    )

    shipping_address_data = models.JSONField(
        null=True,
        blank=True,
        help_text="订单中确认使用的收货地址。",
    )

    billing_address_data = models.JSONField(
        null=True,
        blank=True,
        help_text="订单中确认使用的账单地址。",
    )

    extraction_error = models.TextField(
        blank=True,
        help_text="如果医院订单提取失败，在这里保存错误信息。",
    )

    extracted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="医院订单提取完成时间。",
    )

    document_validation_status = models.CharField(
        max_length=50,
        choices=DocumentValidationStatus.choices,
        default=DocumentValidationStatus.NOT_CHECKED,
        help_text="生成 Invoice / PO 前的校验状态。",
    )

    document_validation_data = models.JSONField(
        null=True,
        blank=True,
        help_text="生成文件前的校验结果，包括 errors 和 warnings。",
    )

    validated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="最后一次生成文件前校验时间。",
    )

    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.HOSPITAL_ORDER_UPLOADED,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_orders",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    notes = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.bon_de_commande} - {self.hospital_name or 'Unknown Hospital'}"


class OrderItem(models.Model):
    """
    医院订单产品行。

    一张医院订单可以包含多个产品。
    OCR 提取后，系统会生成这些 OrderItem。
    后续工厂确认数量、缺货数量也会记录在这里。
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        CONFIRMED = "confirmed", "Confirmed"
        PARTIALLY_CONFIRMED = "partially_confirmed", "Partially confirmed"
        BACKORDERED = "backordered", "Backordered"
        CANCELLED = "cancelled", "Cancelled"

    class ProductMatchStatus(models.TextChoices):
        OK = "ok", "OK"
        NEEDS_REVIEW = "needs_review", "Needs review"
        MANUALLY_CONFIRMED = "manually_confirmed", "Manually confirmed"
        
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )

    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
        help_text="匹配到的产品数据库记录。",
    )

    product_match_status = models.CharField(
        max_length=50,
        choices=ProductMatchStatus.choices,
        default=ProductMatchStatus.NEEDS_REVIEW,
        help_text="产品编号和产品库的匹配状态。",
    )

    product_match_message = models.TextField(
        blank=True,
        help_text="产品匹配说明。如果需要人工确认，会写在这里。",
    )

    is_manually_confirmed = models.BooleanField(
        default=False,
        help_text="人工确认该产品行无误后勾选。",
    )

    product_code = models.CharField(
        max_length=100,
        db_index=True,
        help_text="订单中提取到的产品编号。",
    )

    description = models.TextField(
        blank=True,
    )

    requested_quantity = models.PositiveIntegerField(
        default=0,
        help_text="医院订单请求数量。",
    )

    confirmed_quantity = models.PositiveIntegerField(
        default=0,
        help_text="工厂确认有货数量。",
    )

    backordered_quantity = models.PositiveIntegerField(
        default=0,
        help_text="缺货数量。",
    )

    hospital_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    factory_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Factory unit price snapshot",
    )

    expiration_discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        verbose_name="Expiration discount rate snapshot",
        help_text="Example: 0.30 means 30% discount, i.e. 70% final price.",
    )

    price_policy = models.ForeignKey(
        "pricing.PricePolicy",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
        verbose_name="Applied price policy",
    )

    price_policy_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Price policy date",
    )

    price_policy_message = models.TextField(
        blank=True,
        default="",
        verbose_name="Price policy message",
    )

    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.REQUESTED,
    )

    raw_data = models.JSONField(
        null=True,
        blank=True,
        help_text="该产品行的原始提取数据。",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.order.bon_de_commande} - {self.product_code}"
