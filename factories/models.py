from django.db import models


class Factory(models.Model):
    """
    工厂库。

    用于生成 Factory Purchase Order。
    后续 FactoryConfirmation 会匹配到这里的工厂。
    """

    name = models.CharField(
        max_length=255,
        help_text="工厂正式名称。",
    )

    legal_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="工厂完整法律名称。",
    )

    short_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="简称，例如 LEPU MEDICAL。",
    )

    address = models.TextField(
        blank=True,
        help_text="工厂地址。",
    )

    buyer = models.CharField(
        max_length=255,
        blank=True,
        help_text="采购联系人 / Buyer。",
    )

    default_product_description = models.CharField(
        max_length=255,
        blank=True,
        default="HT-Supreme™ Drug Eluting Stent",
        help_text="生成工厂 PO 时的默认产品描述。",
    )

    match_keywords = models.TextField(
        blank=True,
        help_text="用于自动匹配的关键词，每行一个。例如工厂简称、英文名、地址关键词。",
    )

    is_active = models.BooleanField(
        default=True,
        help_text="是否启用。",
    )

    notes = models.TextField(
        blank=True,
        help_text="备注。",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Factory"
        verbose_name_plural = "Factories"
        ordering = ["name"]

    def __str__(self):
        return self.short_name or self.name