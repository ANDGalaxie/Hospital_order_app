from datetime import date

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from factories.models import Factory
from products.models import ProductCategory


class PricePolicy(models.Model):
    """
    历史价格规则。

    用途：
      - 根据医院订单日期，决定医院卖价、工厂采购价、有效期折扣率。
      - 以后价格变化，只需要新增规则，不需要改代码。

    说明：
      - expiration_discount_rate = 0.30 表示 30% discount，也就是打 7 折。
      - expiration_discount_rate = 0.20 表示 20% discount，也就是打 8 折。
    """

    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Rule name",
    )

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="price_policies",
        verbose_name="Factory",
        help_text="Leave empty only if this rule applies to any factory.",
    )

    category = models.ForeignKey(
        ProductCategory,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="price_policies",
        verbose_name="Product category",
        help_text="Leave empty only if this rule applies to any product category.",
    )

    start_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Start date",
        help_text="Inclusive. Empty means no lower limit.",
    )

    end_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="End date",
        help_text="Inclusive. Empty means still active.",
    )

    hospital_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Hospital selling price",
    )

    factory_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Factory purchase price",
    )

    expiration_discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        verbose_name="Expiration discount rate",
        help_text="Example: 0.30 means 30% discount, i.e. 70% final price.",
    )

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["factory__name", "category__name", "-start_date"]
        verbose_name = "Price Policy"
        verbose_name_plural = "Price Policies"

    def __str__(self):
        factory_name = self.factory.short_name if self.factory and self.factory.short_name else (
            self.factory.name if self.factory else "Any factory"
        )
        category_name = self.category.get_full_path() if self.category else "Any category"
        start = self.start_date.isoformat() if self.start_date else "beginning"
        end = self.end_date.isoformat() if self.end_date else "future"
        return f"{factory_name} / {category_name} / {start} - {end}"

    def clean(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("Start date cannot be later than end date.")

        if not self.is_active:
            return

        qs = PricePolicy.objects.filter(
            is_active=True,
            factory=self.factory,
            category=self.category,
        ).exclude(pk=self.pk)

        for other in qs:
            if date_ranges_overlap(
                self.start_date,
                self.end_date,
                other.start_date,
                other.end_date,
            ):
                raise ValidationError(
                    "Another active price policy already overlaps with this date range "
                    "for the same factory and category."
                )


def date_ranges_overlap(start1, end1, start2, end2):
    """
    None start = -infinity
    None end = +infinity
    """
    min_date = date(1900, 1, 1)
    max_date = date(2999, 12, 31)

    s1 = start1 or min_date
    e1 = end1 or max_date
    s2 = start2 or min_date
    e2 = end2 or max_date

    return s1 <= e2 and s2 <= e1