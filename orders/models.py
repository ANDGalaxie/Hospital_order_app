from django.conf import settings
from django.db import models


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

# Create your models here.
