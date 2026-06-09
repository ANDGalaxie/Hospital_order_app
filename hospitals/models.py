from django.db import models


class Hospital(models.Model):
    """
    医院主数据表。

    以后系统提取到医院名称或地址后，会尝试匹配这里的医院记录。
    Invoice 的 billing address 也应该优先来自这里，而不是完全依赖 OCR。
    """

    name = models.CharField(
        max_length=255,
        unique=True,
        help_text="医院正式名称。",
    )

    normalized_name = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text="用于模糊匹配的标准化名称。",
    )

    billing_address = models.TextField(
        blank=True,
        help_text="发票地址 / billing address。",
    )

    default_shipping_address = models.TextField(
        blank=True,
        help_text="默认收货地址。如果订单 PDF 里有收货地址，则优先使用订单 PDF。",
    )

    contact_name = models.CharField(
        max_length=255,
        blank=True,
    )

    phone = models.CharField(
        max_length=100,
        blank=True,
    )

    fax = models.CharField(
        max_length=100,
        blank=True,
    )

    email = models.EmailField(
        blank=True,
    )

    notes = models.TextField(
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name