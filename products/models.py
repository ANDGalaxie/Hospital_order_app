from decimal import Decimal

from django.db import models


class Product(models.Model):
    """
    产品数据库。

    这里以后会从你的 product_database.xlsx 导入。
    医院订单 OCR 提取出来的产品编号，会匹配这里。
    """

    code = models.CharField(
        max_length=100,
        unique=True,
        help_text="产品编号，例如 BMA-2.5010。",
    )

    description = models.TextField(
        blank=True,
        help_text="产品描述。",
    )

    hospital_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("270.00"),
        help_text="卖给医院的单价。",
    )

    factory_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("120.00"),
        help_text="从工厂采购的单价。当前规则通常为 120.00。",
    )

    is_active = models.BooleanField(
        default=True,
    )

    notes = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code