from django.db import models
from factories.models import Factory


class ProductCategory(models.Model):
    """
    产品分类树。

    例如：
        心血管外周
            SINOMED
                支架
            LEPU MEDICAL
                NC Balloon
                SC Balloon
                Cutting Balloon
                IVL Balloon
                Guide Wire
        眼科
        骨科
    """

    class NodeType(models.TextChoices):
        """
        产品库分类树中的节点类型。

        固定层级：
            department：科室
            factory：该科室下的工厂入口
            category：工厂下的具体产品分类
        """

        DEPARTMENT = "department", "科室"
        FACTORY = "factory", "工厂"
        CATEGORY = "category", "产品分类"

    node_type = models.CharField(
        max_length=20,
        choices=NodeType.choices,
        default=NodeType.CATEGORY,
        db_index=True,
        help_text="该节点在产品库中的层级类型。",
    )

    name = models.CharField(
        max_length=255,
        help_text="分类名称。",
    )

    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        help_text="上级分类。没有上级时表示一级科室。",
    )

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_category_nodes",
        help_text=(
            "仅工厂节点使用。关联到真实 Factory 记录，"
            "避免仅依赖工厂名称进行匹配。"
        ),
    )

    sort_order = models.PositiveIntegerField(
        default=0,
        help_text="同一层级中的显示顺序，数字越小越靠前。",
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
        verbose_name = "Product category"
        verbose_name_plural = "Product categories"
        ordering = ["parent__name", "sort_order", "name"]

    def get_full_path(self):
        parts = [self.name]
        current = self.parent

        while current:
            parts.append(current.name)
            current = current.parent

        return " / ".join(reversed(parts))

    def __str__(self):
        return self.get_full_path()


class Product(models.Model):
    code = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="产品编号，例如 BMA-2.2510。",
    )

    description = models.TextField(
        blank=True,
        help_text="产品描述。",
    )

    category = models.ForeignKey(
        ProductCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
        help_text="产品分类，例如 心血管外周 / SINOMED / 支架。",
    )

    factory = models.ForeignKey(
        Factory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
        help_text="产品所属供应商 / 工厂。",
    )

    hospital_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        default=None,
        help_text="卖给医院的单价。",
    )

    factory_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        default=None,
        help_text="从工厂采购的单价。",
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

class ProductBrowserCategory(ProductCategory):
    """
    Admin 中用于“产品库浏览”的代理模型。

    不创建新表，只是让 Django Admin 多一个更友好的入口：
        Products
        ↓
        科室
        ↓
        供应商
        ↓
        产品分类
        ↓
        具体产品
    """

    class Meta:
        proxy = True
        verbose_name = "Product records"
        verbose_name_plural = "Product records"