from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode

from backorders.models import (
    BackorderRootFolder,
    BackorderOrderFolder,
    BackorderLine,
    ExpectedShippingMonthFolder,
    ExpectedShippingProductFolder,
)
from backorders.services.backorder_sync_service import (
    sync_backorders_for_all_orders,
    rebuild_expected_shipping_folders,
)


@admin.register(BackorderRootFolder)
class BackorderRootFolderAdmin(admin.ModelAdmin):
    """
    入口：
      待发产品库
        待发产品
        预计可发产品
    """

    list_display = (
        "name",
        "open_folder",
    )

    actions = [
        "recalculate_backorder_library",
    ]

    def open_folder(self, obj):
        if obj.code == BackorderRootFolder.FolderCode.BACKORDER_ORDERS:
            url = (
                reverse("admin:backorders_backorderorderfolder_changelist")
                + "?is_active__exact=1"
            )
            return format_html('<a href="{}">Open</a>', url)

        if obj.code == BackorderRootFolder.FolderCode.EXPECTED_SHIPPING:
            url = reverse("admin:backorders_expectedshippingmonthfolder_changelist")
            return format_html('<a href="{}">Open</a>', url)

        return "-"

    open_folder.short_description = "Open"

    @admin.action(description="Recalculate backorder library")
    def recalculate_backorder_library(self, request, queryset):
        count = sync_backorders_for_all_orders()
        self.message_user(
            request,
            f"Backorder library recalculated for {count} order(s).",
            level=messages.SUCCESS,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BackorderOrderFolder)
class BackorderOrderFolderAdmin(admin.ModelAdmin):
    """
    第一分支：
      待发产品
        Order 150222
          BackorderLine
    """

    list_display = (
        "order_bon",
        "hospital",
        "line_count",
        "remaining_total_quantity",
        "earliest_expected_shipping_date",
        "last_calculated_at",
        "open_lines",
    )

    list_filter = (
        "is_active",
        "earliest_expected_shipping_date",
    )

    search_fields = (
        "order__bon_de_commande",
        "order__hospital__name",
    )

    def order_bon(self, obj):
        return obj.order.bon_de_commande

    order_bon.short_description = "Order"

    def hospital(self, obj):
        if obj.order.hospital:
            return obj.order.hospital.name
        return "-"

    hospital.short_description = "Hospital"

    def open_lines(self, obj):
        query = urlencode({
            "order_folder__id__exact": obj.id,
            "is_active__exact": "1",
        })
        url = reverse("admin:backorders_backorderline_changelist") + f"?{query}"
        return format_html('<a href="{}">Open products</a>', url)

    open_lines.short_description = "Products"

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(BackorderLine)
class BackorderLineAdmin(admin.ModelAdmin):
    """
    订单里的待发产品明细。
    这里可以手动填写预计发货时间。
    """

    list_display = (
        "order_bon",
        "product_code",
        "remaining_quantity",
        "requested_quantity",
        "shipped_quantity",
        "expected_shipping_date",
        "expected_shipping_note",
        "status",
    )

    list_editable = (
        "expected_shipping_date",
        "expected_shipping_note",
    )

    list_filter = (
        "is_active",
        "status",
        "expected_month_key",
        "expected_shipping_date",
    )

    search_fields = (
        "order__bon_de_commande",
        "product_code",
        "description",
        "expected_shipping_note",
    )

    readonly_fields = (
        "order_folder",
        "order",
        "product",
        "product_code",
        "description",
        "requested_quantity",
        "shipped_quantity",
        "remaining_quantity",
        "expected_month_key",
        "status",
        "is_active",
        "last_calculated_at",
        "created_at",
        "updated_at",
    )

    fields = (
        "order_folder",
        "order",
        "product",
        "product_code",
        "description",
        "requested_quantity",
        "shipped_quantity",
        "remaining_quantity",
        "expected_shipping_date",
        "expected_shipping_note",
        "expected_month_key",
        "status",
        "is_active",
        "last_calculated_at",
        "created_at",
        "updated_at",
    )

    def order_bon(self, obj):
        return obj.order.bon_de_commande

    order_bon.short_description = "Order"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        rebuild_expected_shipping_folders()

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(ExpectedShippingMonthFolder)
class ExpectedShippingMonthFolderAdmin(admin.ModelAdmin):
    """
    第二分支：
      预计可发产品
        2026-09
          产品汇总
    """

    list_display = (
        "display_name",
        "product_count",
        "line_count",
        "total_remaining_quantity",
        "last_calculated_at",
        "open_products",
    )

    search_fields = (
        "month_key",
        "display_name",
    )

    def open_products(self, obj):
        query = urlencode({
            "month_folder__id__exact": obj.id,
        })
        url = reverse("admin:backorders_expectedshippingproductfolder_changelist") + f"?{query}"
        return format_html('<a href="{}">Open products</a>', url)

    open_products.short_description = "Products"

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(ExpectedShippingProductFolder)
class ExpectedShippingProductFolderAdmin(admin.ModelAdmin):
    """
    某个月下，按产品号汇总。
    """

    list_display = (
        "month_folder",
        "product_code",
        "total_remaining_quantity",
        "order_count",
        "line_count",
        "open_orders",
    )

    list_filter = (
        "month_folder",
    )

    search_fields = (
        "product_code",
    )

    def open_orders(self, obj):
        query = urlencode({
            "product_code__exact": obj.product_code,
            "expected_month_key__exact": obj.month_folder.month_key,
            "is_active__exact": "1",
        })
        url = reverse("admin:backorders_backorderline_changelist") + f"?{query}"
        return format_html('<a href="{}">Open orders</a>', url)

    open_orders.short_description = "Orders"

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False