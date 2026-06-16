from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from shipments.models import (
    ShipmentMonth,
    ShipmentOrderFolder,
    ShipmentBatch,
    ShipmentBatchItem,
    BackorderSnapshotItem,
)


@admin.register(ShipmentMonth)
class ShipmentMonthAdmin(admin.ModelAdmin):
    """
    第一层：月份。
    左侧点 Shipment Batches 后，首先只看到月份。
    """

    list_display = (
        "month_key",
        "order_count",
        "batch_count",
        "remaining_total",
        "open_orders",
    )

    search_fields = ("month_key",)

    def order_count(self, obj):
        return obj.order_folders.count()

    order_count.short_description = "Orders"

    def batch_count(self, obj):
        return ShipmentBatch.objects.filter(order_folder__month=obj).count()

    batch_count.short_description = "Batches"

    def remaining_total(self, obj):
        total = 0
        batches = ShipmentBatch.objects.filter(order_folder__month=obj)

        for batch in batches:
            total += int(batch.remaining_after_batch_quantity or 0)

        return total

    remaining_total.short_description = "Remaining"

    def open_orders(self, obj):
        url = (
            reverse("admin:shipments_shipmentorderfolder_changelist")
            + f"?month__id__exact={obj.id}"
        )
        return format_html('<a href="{}">Open orders</a>', url)

    open_orders.short_description = "Open"


@admin.register(ShipmentOrderFolder)
class ShipmentOrderFolderAdmin(admin.ModelAdmin):
    """
    第二层：某个月里的订单。
    """

    list_display = (
        "month",
        "order_bon",
        "hospital_name",
        "batch_count",
        "latest_batch_date",
        "remaining_total",
        "open_batches",
    )

    list_filter = ("month",)
    search_fields = (
        "order__bon_de_commande",
        "order__hospital_name",
    )

    def order_bon(self, obj):
        return obj.order.bon_de_commande

    order_bon.short_description = "Order"

    def hospital_name(self, obj):
        return obj.order.hospital_name or "-"

    hospital_name.short_description = "Hospital"

    def batch_count(self, obj):
        return obj.batches.count()

    batch_count.short_description = "Batches"

    def latest_batch_date(self, obj):
        batch = obj.batches.order_by("-batch_date", "-id").first()
        return batch.batch_date if batch else "-"

    latest_batch_date.short_description = "Latest batch date"

    def remaining_total(self, obj):
        latest = obj.batches.order_by("-batch_date", "-id").first()
        if not latest:
            return "-"
        return latest.remaining_after_batch_quantity

    remaining_total.short_description = "Remaining"

    def open_batches(self, obj):
        url = (
            reverse("admin:shipments_shipmentbatch_changelist")
            + f"?order_folder__id__exact={obj.id}"
        )
        return format_html('<a href="{}">Open batches</a>', url)

    open_batches.short_description = "Open"

    def has_module_permission(self, request):
        """
        不在左侧单独显示。
        只能从 ShipmentMonth 点进去。
        """
        return False


class ShipmentBatchItemInline(admin.TabularInline):
    model = ShipmentBatchItem
    extra = 0
    can_delete = False
    readonly_fields = (
        "product",
        "product_code",
        "shipped_quantity",
    )


class BackorderSnapshotItemInline(admin.TabularInline):
    model = BackorderSnapshotItem
    extra = 0
    can_delete = False
    readonly_fields = (
        "product",
        "product_code",
        "requested_quantity",
        "shipped_before_batch_quantity",
        "shipped_this_batch_quantity",
        "total_shipped_after_batch_quantity",
        "remaining_quantity",
        "is_over_shipped",
    )


@admin.register(ShipmentBatch)
class ShipmentBatchAdmin(admin.ModelAdmin):
    """
    第三层：某个订单下的发货批次。
    """

    list_display = (
        "month_key",
        "order",
        "batch_number",
        "batch_date",
        "status",
        "shipped_this_batch_quantity",
        "remaining_after_batch_quantity",
        "open_shipped_items",
        "open_backorder_items",
    )

    list_filter = (
        "month_key",
        "status",
        "batch_date",
        "order_folder",
    )

    search_fields = (
        "order__bon_de_commande",
        "shipped_items__product_code",
        "backorder_items__product_code",
    )

    readonly_fields = (
        "order",
        "order_folder",
        "factory_confirmation",
        "batch_number",
        "batch_date",
        "month_key",
        "status",
        "total_requested_quantity",
        "shipped_this_batch_quantity",
        "total_shipped_after_batch_quantity",
        "remaining_after_batch_quantity",
        "created_at",
        "updated_at",
    )

    inlines = [
        ShipmentBatchItemInline,
        BackorderSnapshotItemInline,
    ]

    def open_shipped_items(self, obj):
        url = (
            reverse("admin:shipments_shipmentbatchitem_changelist")
            + f"?batch__id__exact={obj.id}"
        )
        return format_html('<a href="{}">Shipped</a>', url)

    open_shipped_items.short_description = "Shipped"

    def open_backorder_items(self, obj):
        url = (
            reverse("admin:shipments_backordersnapshotitem_changelist")
            + f"?batch__id__exact={obj.id}"
        )
        return format_html('<a href="{}">Backorder</a>', url)

    open_backorder_items.short_description = "Backorder"

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        """
        不在左侧单独显示。
        只能从订单文件夹点进去。
        """
        return False


@admin.register(ShipmentBatchItem)
class ShipmentBatchItemAdmin(admin.ModelAdmin):
    """
    第四层 A：这一批已发产品。
    """

    list_display = (
        "batch",
        "product_code",
        "shipped_quantity",
    )

    list_filter = (
        "batch__month_key",
        "batch__status",
    )

    search_fields = (
        "product_code",
        "batch__order__bon_de_commande",
    )

    readonly_fields = (
        "batch",
        "product",
        "product_code",
        "shipped_quantity",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        return False


@admin.register(BackorderSnapshotItem)
class BackorderSnapshotItemAdmin(admin.ModelAdmin):
    """
    第四层 B：这一批之后待发产品。
    """

    list_display = (
        "batch",
        "product_code",
        "requested_quantity",
        "shipped_before_batch_quantity",
        "shipped_this_batch_quantity",
        "total_shipped_after_batch_quantity",
        "remaining_quantity",
        "is_over_shipped",
    )

    list_filter = (
        "batch__month_key",
        "is_over_shipped",
    )

    search_fields = (
        "product_code",
        "batch__order__bon_de_commande",
    )

    readonly_fields = (
        "batch",
        "product",
        "product_code",
        "requested_quantity",
        "shipped_before_batch_quantity",
        "shipped_this_batch_quantity",
        "total_shipped_after_batch_quantity",
        "remaining_quantity",
        "is_over_shipped",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        return False