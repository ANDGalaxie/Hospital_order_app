from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models

from factories.models import Factory
from products.models import ProductCategory


def date_ranges_overlap(start1, end1, start2, end2):
    """
    日期范围首尾均包含。

    None start = 无下限
    None end = 无上限
    """
    min_date = date(1900, 1, 1)
    max_date = date(2999, 12, 31)

    s1 = start1 or min_date
    e1 = end1 or max_date
    s2 = start2 or min_date
    e2 = end2 or max_date

    return s1 <= e2 and s2 <= e1


def get_category_factory(category):
    """
    从产品分类向上查找其工厂节点所关联的真实 Factory。
    """
    current = category
    visited_ids = set()

    while current is not None:
        if current.id in visited_ids:
            break

        visited_ids.add(current.id)

        if (
            current.node_type
            == ProductCategory.NodeType.FACTORY
        ):
            return current.factory

        current = current.parent

    return None


class PricePolicy(models.Model):
    """
    历史价格规则。

    价格阶段判断日期：
        医院订单 Date de commande

    临期折扣判断日期：
        由调用方显式提供 reference_date。
        正式 Factory PO 应使用 PO document date。
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
        help_text=(
            "留空表示全局通用规则。"
        ),
    )

    category = models.ForeignKey(
        ProductCategory,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="price_policies",
        verbose_name="Product category",
        help_text=(
            "留空表示适用于该工厂下所有产品。"
        ),
    )

    start_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Start date",
        help_text="包含该日期。留空表示无下限。",
    )

    end_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="End date",
        help_text="包含该日期。留空表示长期有效。",
    )

    hospital_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Hospital selling price",
        validators=[
            MinValueValidator(Decimal("0.01")),
        ],
    )

    factory_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Factory purchase price",
        validators=[
            MinValueValidator(Decimal("0.01")),
        ],
    )

    expiration_discount_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        verbose_name="Expiration discount rate",
        help_text=(
            "0.30 表示优惠 30%，最终支付原价的 70%。"
        ),
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("1.00")),
        ],
    )

    expiration_threshold_days = models.PositiveIntegerField(
        default=365,
        verbose_name="Expiration threshold days",
        help_text=(
            "例如 365 表示有效期距离基准日期不足 "
            "365 天时应用临期折扣。"
        ),
    )

    is_active = models.BooleanField(default=True)

    notes = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "factory__name",
            "category__name",
            "-start_date",
        ]
        verbose_name = "Price Policy"
        verbose_name_plural = "Price Policies"

    def __str__(self):
        if self.factory:
            factory_name = (
                self.factory.short_name
                or self.factory.name
            )
        else:
            factory_name = "Any factory"

        category_name = (
            self.category.get_full_path()
            if self.category
            else "Any category"
        )

        start = (
            self.start_date.isoformat()
            if self.start_date
            else "beginning"
        )

        end = (
            self.end_date.isoformat()
            if self.end_date
            else "future"
        )

        return (
            f"{factory_name} / {category_name} / "
            f"{start} - {end}"
        )

    def clean(self):
        errors = {}

        if (
            self.start_date
            and self.end_date
            and self.start_date > self.end_date
        ):
            errors["end_date"] = (
                "结束日期不能早于开始日期。"
            )

        if (
            self.expiration_discount_rate
            and self.expiration_discount_rate > 0
            and self.expiration_threshold_days <= 0
        ):
            errors["expiration_threshold_days"] = (
                "设置临期折扣时，门槛天数必须大于 0。"
            )

        if self.category_id:
            if not self.factory_id:
                errors["factory"] = (
                    "选择产品分类时必须同时选择工厂。"
                )

            elif (
                self.category.node_type
                != ProductCategory.NodeType.CATEGORY
            ):
                errors["category"] = (
                    "价格规则只能选择真正的产品分类。"
                    "工厂通用规则请将产品分类留空。"
                )

            else:
                category_factory = get_category_factory(
                    self.category
                )

                if category_factory is None:
                    errors["category"] = (
                        "该产品分类没有关联真实工厂。"
                    )

                elif category_factory.id != self.factory_id:
                    errors["category"] = (
                        "所选产品分类不属于所选工厂。"
                    )

        if errors:
            raise ValidationError(errors)

        if not self.is_active:
            return

        same_scope_policies = (
            PricePolicy.objects.filter(
                is_active=True,
                factory=self.factory,
                category=self.category,
            )
            .exclude(pk=self.pk)
            .order_by()
        )

        for other in same_scope_policies:
            if date_ranges_overlap(
                self.start_date,
                self.end_date,
                other.start_date,
                other.end_date,
            ):
                raise ValidationError(
                    {
                        "__all__": (
                            "同一工厂和产品分类下，"
                            "已经存在日期范围重叠的启用规则："
                            f"{other}。"
                        )
                    }
                )
