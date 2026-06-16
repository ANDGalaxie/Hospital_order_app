from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode

from .models import Product, ProductCategory, ProductBrowserCategory


@admin.register(ProductBrowserCategory)
class ProductBrowserCategoryAdmin(admin.ModelAdmin):
    """
    文件夹式产品库浏览入口。

    默认只显示一级科室：
        心血管外周 / 眼科 / 骨科

    点击 Open 后进入下一层：
        SINOMED / LEPU MEDICAL

    再点击 Open 后进入产品分类：
        支架 / NC Balloon / SC Balloon ...

    点击 View products 后进入具体产品列表。
    """

    list_display = (
        "name",
        "full_path",
        "children_count",
        "direct_product_count",
        "open_children",
        "view_products",
    )

    search_fields = (
        "name",
        "parent__name",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    actions = None

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        parent_id = request.GET.get("parent_id")

        if parent_id:
            return qs.filter(parent_id=parent_id).order_by("name")

        return qs.filter(parent__isnull=True).order_by("name")

    def full_path(self, obj):
        return obj.get_full_path()

    full_path.short_description = "Path"

    def children_count(self, obj):
        return obj.children.count()

    children_count.short_description = "Subfolders"

    def direct_product_count(self, obj):
        return obj.products.count()

    direct_product_count.short_description = "Products"

    def open_children(self, obj):
        if obj.children.count() == 0:
            return "-"

        opts = self.model._meta
        url = reverse(
            f"admin:{opts.app_label}_{opts.model_name}_changelist"
        )

        query = urlencode({"parent_id": obj.id})

        return format_html(
            '<a class="button" href="{}?{}">Open</a>',
            url,
            query,
        )

    open_children.short_description = "Open"

    def view_products(self, obj):
        product_count = obj.products.count()

        if product_count == 0:
            return "-"

        url = reverse("admin:products_product_changelist")
        query = urlencode({"category__id__exact": obj.id})

        return format_html(
            '<a class="button" href="{}?{}">View products ({})</a>',
            url,
            query,
            product_count,
        )

    view_products.short_description = "Product list"

    def has_delete_permission(self, request, obj=None):
        """
        先不允许从浏览入口删除分类，避免误删整个分类树。
        如果以后需要删除，可以去数据库或单独开放。
        """
        return False


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """
    具体产品记录列表。

    这里显示真正的产品编号、描述、价格、供应商、分类。
    """

    list_display = (
        "code",
        "description",
        "category_path",
        "factory",
        "hospital_unit_price",
        "factory_unit_price",
        "is_active",
    )

    list_filter = (
        "category",
        "factory",
        "is_active",
    )

    search_fields = (
        "code",
        "description",
        "category__name",
        "factory__name",
        "factory__short_name",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            "Product information",
            {
                "fields": (
                    "code",
                    "description",
                    "category",
                    "factory",
                    "is_active",
                    "notes",
                )
            },
        ),
        (
            "Prices",
            {
                "fields": (
                    "hospital_unit_price",
                    "factory_unit_price",
                )
            },
        ),
        (
            "System information",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def category_path(self, obj):
        if obj.category:
            return obj.category.get_full_path()
        return "-"

    category_path.short_description = "Category"

    def has_module_permission(self, request):
        return False