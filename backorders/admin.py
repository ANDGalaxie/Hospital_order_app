from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode
from urllib.parse import urlencode

from backorders.models import (
    BackorderRootFolder,
    BackorderOrderFolder,
    BackorderLine,
    ExpectedShippingMonthFolder,
    ExpectedShippingProductFolder,
    InventoryAllocation,
    InventoryBatch,
    InventoryItem,
    InventoryProductFolder,
)

from backorders.services.backorder_sync_service import (
    sync_backorders_for_all_orders,
    rebuild_expected_shipping_folders,
)

from backorders.services.inventory_service import (
    extract_inventory_batch,
    rebuild_inventory_product_folders,
    get_allocatable_orders_for_product,
    allocate_inventory_to_order,
    cancel_inventory_allocation,
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

        if obj.code == BackorderRootFolder.FolderCode.INVENTORY_PRODUCTS:
            product_url = reverse("admin:backorders_inventoryproductfolder_changelist")
            batch_url = reverse("admin:backorders_inventorybatch_changelist")
            allocation_url = reverse("admin:backorders_inventoryallocation_changelist")
            add_batch_url = reverse("admin:backorders_inventorybatch_add")

            return format_html(
                '<a href="{}">查看库存产品</a> &nbsp;|&nbsp; '
                '<a href="{}">查看库存批次</a> &nbsp;|&nbsp; '
                '<a href="{}">查看预留记录</a> &nbsp;|&nbsp; '
                '<a href="{}">新增库存批次</a>',
                product_url,
                batch_url,
                allocation_url,
                add_batch_url,
            )

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

class InventoryItemInline(admin.TabularInline):
    model = InventoryItem
    extra = 0
    can_delete = False
    readonly_fields = (
        "product",
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "allocated_order",
        "allocated_at",
    )

    fields = (
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "allocated_order",
        "allocated_at",
    )


@admin.register(InventoryBatch)
class InventoryBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch_name",
        "factory",
        "batch_date",
        "extraction_status",
        "item_count",
        "created_by",
        "created_at",
    )

    list_filter = (
        "extraction_status",
        "factory",
        "batch_date",
        "created_at",
    )

    search_fields = (
        "batch_name",
        "factory__name",
        "factory__short_name",
        "items__product_code",
        "items__serial_number",
    )

    readonly_fields = (
        "extraction_status",
        "extracted_data",
        "extraction_error",
        "extracted_at",
        "created_at",
        "updated_at",
    )

    fields = (
        "batch_name",
        "factory",
        "source_pdf",
        "batch_date",
        "notes",
        "created_by",
        "extraction_status",
        "extracted_data",
        "extraction_error",
        "extracted_at",
        "created_at",
        "updated_at",
    )

    actions = [
        "extract_selected_inventory_batches",
        "rebuild_inventory_summary",
    ]

    inlines = [
        InventoryItemInline,
    ]

    def item_count(self, obj):
        return obj.items.count()

    item_count.short_description = "库存数量"

    @admin.action(description="提取所选库存批次")
    def extract_selected_inventory_batches(self, request, queryset):
        success = 0
        failed = 0

        for batch in queryset:
            try:
                extract_inventory_batch(batch)
                success += 1
                self.message_user(
                    request,
                    f"库存批次 {batch.id} 提取成功。",
                    level=messages.SUCCESS,
                )
            except Exception as exc:
                failed += 1
                self.message_user(
                    request,
                    f"库存批次 {batch.id} 提取失败：{exc}",
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            f"库存批次提取完成：成功 {success}，失败 {failed}。",
            level=messages.INFO,
        )

    @admin.action(description="重建库存产品汇总")
    def rebuild_inventory_summary(self, request, queryset):
        rebuild_inventory_product_folders()
        self.message_user(
            request,
            "库存产品汇总已重建。",
            level=messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change):
        if not obj.created_by:
            obj.created_by = request.user

        super().save_model(request, obj, form, change)

    def has_module_permission(self, request):
        return False


@admin.register(InventoryProductFolder)
class InventoryProductFolderAdmin(admin.ModelAdmin):
    list_display = (
        "product_code",
        "available_quantity",
        "allocated_quantity",
        "reserved_quantity",
        "cancelled_quantity",
        "total_quantity",
        "earliest_expiration_date",
        "open_inventory_items",
    )

    search_fields = (
        "product_code",
        "product__description",
    )

    readonly_fields = (
        "product",
        "product_code",
        "total_quantity",
        "available_quantity",
        "reserved_quantity",
        "allocated_quantity",
        "cancelled_quantity",
        "earliest_expiration_date",
        "last_calculated_at",
        "available_serials_table",
        "allocatable_orders_table",
    )

    fields = (
        "product",
        "product_code",
        "total_quantity",
        "available_quantity",
        "reserved_quantity",
        "allocated_quantity",
        "cancelled_quantity",
        "earliest_expiration_date",
        "last_calculated_at",
        "available_serials_table",
        "allocatable_orders_table",
    )

    actions = [
        "rebuild_inventory_summary",
    ]

    def open_inventory_items(self, obj):
        url = (
            reverse("admin:backorders_inventoryitem_changelist")
            + f"?product_code__exact={obj.product_code}&status__exact=available"
        )
        return format_html('<a href="{}">查看可用 Serials</a>', url)

    open_inventory_items.short_description = "库存明细"

    def available_serials_table(self, obj):
        items = (
            InventoryItem.objects
            .filter(
                product_code=obj.product_code,
                status=InventoryItem.Status.AVAILABLE,
            )
            .order_by("expiration_date", "serial_number")[:50]
        )

        if not items:
            return "当前没有可用库存。"

        rows = ""

        for item in items:
            rows += (
                "<tr>"
                f"<td>{item.serial_number}</td>"
                f"<td>{item.expiration_date or '-'}</td>"
                f"<td>{item.batch}</td>"
                "</tr>"
            )

        html = f"""
        <table style="width:100%; border-collapse:collapse;">
            <thead>
                <tr>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">Serial Number</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">有效期</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">库存批次</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        """

        return format_html(html)

    available_serials_table.short_description = "可用库存 Serials"

    def allocatable_orders_table(self, obj):
        orders = get_allocatable_orders_for_product(obj.product_code)

        if not orders:
            return "当前没有待发该产品的订单。"

        rows = ""

        for order in orders:
            params = urlencode(
                {
                    "product_code": obj.product_code,
                    "order": order["order_id"],
                }
            )

            allocation_url = (
                reverse("admin:backorders_inventoryallocation_add")
                + f"?{params}"
            )

            rows += (
                "<tr>"
                f"<td>Order {order['bon_de_commande']}</td>"
                f"<td>{order['order_date_display']}</td>"
                f"<td>{order['hospital_name']}</td>"
                f"<td>{order['remaining_quantity']}</td>"
                f"<td>{order['expected_shipping_date_display']}</td>"
                f'<td><a class="button" href="{allocation_url}">创建预留</a></td>'
                "</tr>"
            )

        html = f"""
        <div style="margin-bottom:8px;">
            <strong>建议优先补发下单日期更早的订单。</strong>
        </div>
        <table style="width:100%; border-collapse:collapse;">
            <thead>
                <tr>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">订单</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">医院下单日期</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">医院</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">待发数量</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">预计发货时间</th>
                    <th style="text-align:left; border-bottom:1px solid #ddd; padding:6px;">操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        """

        return format_html(html)

    allocatable_orders_table.short_description = "可分配订单"

    @admin.action(description="重建库存产品汇总")
    def rebuild_inventory_summary(self, request, queryset):
        rebuild_inventory_product_folders()
        self.message_user(
            request,
            "库存产品汇总已重建。",
            level=messages.SUCCESS,
        )

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = (
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "batch",
        "allocated_order",
        "allocation",
        "allocated_at",
    )

    list_filter = (
        "status",
        "product_code",
        "expiration_date",
        "batch",
    )

    search_fields = (
        "product_code",
        "serial_number",
        "allocated_order__bon_de_commande",
    )

    readonly_fields = (
        "batch",
        "product",
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "allocated_order",
        "allocation",
        "allocated_at",
        "raw_data",
        "created_at",
    )

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False

class InventoryAllocationItemInline(admin.TabularInline):
    model = InventoryItem
    fk_name = "allocation"
    extra = 0
    can_delete = False

    readonly_fields = (
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "batch",
    )

    fields = (
        "product_code",
        "serial_number",
        "expiration_date",
        "status",
        "batch",
    )


@admin.register(InventoryAllocation)
class InventoryAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order",
        "order_date_display",
        "product_code",
        "quantity_requested",
        "allocated_count",
        "status",
        "created_by",
        "created_at",
    )

    list_filter = (
        "status",
        "product_code",
        "created_at",
    )

    search_fields = (
        "order__bon_de_commande",
        "product_code",
        "items__serial_number",
    )

    readonly_fields = (
        "product",
        "allocated_count",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    )

    fields = (
        "order",
        "product_code",
        "quantity_requested",
        "notes",
        "product",
        "allocated_count",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    )

    raw_id_fields = (
        "order",
    )

    actions = [
        "cancel_selected_allocations",
    ]

    inlines = [
        InventoryAllocationItemInline,
    ]

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)

        product_code = request.GET.get("product_code")
        order_id = request.GET.get("order")

        if product_code:
            initial["product_code"] = product_code

        if order_id:
            initial["order"] = order_id

        return initial

    def order_date_display(self, obj):
        order = obj.order

        try:
            from backorders.services.inventory_service import get_order_date_for_display

            order_date = get_order_date_for_display(order)
            if order_date:
                return order_date.strftime("%Y-%m-%d")
        except Exception:
            pass

        return "-"

    order_date_display.short_description = "医院下单日期"

    def save_model(self, request, obj, form, change):
        """
        新增时，不直接保存 obj。
        而是调用 allocate_inventory_to_order，让系统自动选择 serial number。
        """
        if change:
            super().save_model(request, obj, form, change)
            return

        allocation = allocate_inventory_to_order(
            product_code=obj.product_code,
            order=obj.order,
            quantity=obj.quantity_requested,
            created_by=request.user,
            notes=obj.notes,
        )

        obj.pk = allocation.pk
        obj.id = allocation.id

        self.message_user(
            request,
            (
                f"已成功为 Order {allocation.order.bon_de_commande} "
                f"预留 {allocation.product_code} x {allocation.allocated_count}。"
            ),
            level=messages.SUCCESS,
        )

    @admin.action(description="取消所选库存预留")
    def cancel_selected_allocations(self, request, queryset):
        success = 0
        failed = 0

        for allocation in queryset:
            try:
                cancel_inventory_allocation(allocation)
                success += 1
            except Exception as exc:
                failed += 1
                self.message_user(
                    request,
                    f"预留记录 {allocation.id} 取消失败：{exc}",
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            f"取消完成：成功 {success}，失败 {failed}。",
            level=messages.INFO,
        )

    def has_module_permission(self, request):
        return False