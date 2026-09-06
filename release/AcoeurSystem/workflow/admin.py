from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from workflow.models import DocumentWorkflowItem

from workflow.services.workflow_sync_service import (
    sync_document_workflow_items_for_all_batches,
    sync_document_workflow_item_for_batch,
)
from workflow.services.workflow_validation_service import (
    validate_document_workflow_items,
)
from workflow.services.workflow_price_policy_service import (
    apply_price_policy_to_workflow_items,
)
from workflow.services.workflow_document_generation_service import (
    generate_documents_for_workflow_items,
)


@admin.register(DocumentWorkflowItem)
class DocumentWorkflowItemAdmin(admin.ModelAdmin):
    """
    Workflow 操作中心。

    这里以后放：
      - validate
      - apply price policy
      - generate invoice / PO

    Phase 4A 先只负责展示和同步。
    """

    list_display = (
        "order_bon",
        "hospital_name",
        "batch_number",
        "source_display",
        "batch_date",
        "total_units",
        "workflow_status",
        "validation_status",
        "validation_summary",
        "invoice_status",
        "invoice_pdf_link",
        "po_status",
        "po_pdf_link",
        "open_batch",
        "updated_at",
    )

    list_filter = (
        "workflow_status",
        "validation_status",
        "invoice_status",
        "po_status",
        "shipment_batch__source_type",
        "shipment_batch__batch_date",
    )

    list_select_related = (
        "order",
        "shipment_batch",
        "invoice_document",
        "po_document",
    )

    search_fields = (
        "order__bon_de_commande",
        "order__hospital_name",
        "shipment_batch__shipped_items__product_code",
    )

    readonly_fields = (
        "order_bon",
        "source_display",
        "open_batch",
        "invoice_document_link",
        "invoice_pdf_link",
        "invoice_html_link",
        "po_document_link",
        "po_pdf_link",
        "po_html_link",
        "shipment_batch",
        "order",
        "workflow_status",
        "validation_status",
        "validation_data",
        "validated_at",
        "invoice_status",
        "po_status",
        "invoice_document",
        "po_document",
        "created_at",
        "updated_at",
    )

    actions = [
        "sync_selected_workflow_items",
        "sync_all_workflow_items",
        "apply_price_policy_to_selected_workflow_items",
        "validate_selected_workflow_items",
        "generate_documents_for_selected_workflow_items",

    ]

    def has_add_permission(self, request):
        return False

    def order_bon(self, obj):
        url = reverse(
            "admin:orders_order_change",
            args=[obj.order_id],
        )

        return format_html(
            '<a href="{}">{}</a>',
            url,
            obj.order.bon_de_commande,
        )

    order_bon.short_description = "订单号"

    def hospital_name(self, obj):
        return obj.order.hospital_name or "-"

    hospital_name.short_description = "医院"

    def batch_number(self, obj):
        return obj.shipment_batch.batch_number

    batch_number.short_description = "Batch"

    def batch_date(self, obj):
        return obj.shipment_batch.batch_date

    batch_date.short_description = "发货日期"

    def source_display(self, obj):
        batch = obj.shipment_batch

        if getattr(batch, "inventory_allocation_id", None):
            url = reverse(
                "admin:backorders_inventoryallocation_change",
                args=[batch.inventory_allocation_id],
            )

            return format_html(
                '<a href="{}">库存分配 #{}</a>',
                url,
                batch.inventory_allocation_id,
            )

        if getattr(batch, "factory_confirmation_id", None):
            url = reverse(
                "admin:factory_confirmations_factoryconfirmation_change",
                args=[batch.factory_confirmation_id],
            )

            return format_html(
                '<a href="{}">工厂确认 #{}</a>',
                url,
                batch.factory_confirmation_id,
            )

        return "-"

    source_display.short_description = "来源"

    def total_units(self, obj):
        total = 0

        for item in obj.shipment_batch.shipped_items.all():
            total += int(item.shipped_quantity or 0)

        return total

    total_units.short_description = "数量"

    def open_batch(self, obj):
        url = reverse(
            "admin:shipments_shipmentbatch_change",
            args=[obj.shipment_batch_id],
        )

        return format_html('<a href="{}">查看 Batch</a>', url)

    open_batch.short_description = "发货记录"

    def invoice_document_link(self, obj):
        if not obj.invoice_document_id:
            return "-"

        url = reverse(
            "admin:documents_hospitalinvoicedocument_change",
            args=[obj.invoice_document_id],
        )

        return format_html(
            '<a href="{}">Invoice 记录 #{}</a>',
            url,
            obj.invoice_document_id,
        )

    invoice_document_link.short_description = "Invoice 记录"


    def po_document_link(self, obj):
        if not obj.po_document_id:
            return "-"

        url = reverse(
            "admin:documents_factorypurchaseorderdocument_change",
            args=[obj.po_document_id],
        )

        return format_html(
            '<a href="{}">PO 记录 #{}</a>',
            url,
            obj.po_document_id,
        )

    po_document_link.short_description = "PO 记录"


    def invoice_pdf_link(self, obj):
        document = obj.invoice_document

        if document and document.pdf_file:
            return format_html(
                '<a href="{}" target="_blank">Invoice PDF</a>',
                document.pdf_file.url,
            )

        return "-"

    invoice_pdf_link.short_description = "Invoice PDF"


    def invoice_html_link(self, obj):
        document = obj.invoice_document

        if document and document.html_file:
            return format_html(
                '<a href="{}" target="_blank">Invoice HTML</a>',
                document.html_file.url,
            )

        return "-"

    invoice_html_link.short_description = "Invoice HTML"


    def po_pdf_link(self, obj):
        document = obj.po_document

        if document and document.pdf_file:
            return format_html(
                '<a href="{}" target="_blank">PO PDF</a>',
                document.pdf_file.url,
            )

        return "-"

    po_pdf_link.short_description = "PO PDF"


    def po_html_link(self, obj):
        document = obj.po_document

        if document and document.html_file:
            return format_html(
                '<a href="{}" target="_blank">PO HTML</a>',
                document.html_file.url,
            )

        return "-"

    po_html_link.short_description = "PO HTML"

    @admin.action(description="同步所有待处理文件")
    def sync_all_workflow_items(self, request, queryset):
        result = sync_document_workflow_items_for_all_batches()

        self.message_user(
            request,
            (
                f"同步完成：新增 {result['created']}，"
                f"已存在/更新 {result['updated']}，"
                f"总计 {result['total']}。"
            ),
            level=messages.SUCCESS,
        )

    @admin.action(description="同步所选待处理文件")
    def sync_selected_workflow_items(self, request, queryset):
        success = 0
        failed = 0

        for item in queryset.select_related("shipment_batch", "order"):
            try:
                sync_document_workflow_item_for_batch(
                    item.shipment_batch
                )
                success += 1

            except Exception as exc:
                failed += 1
                self.message_user(
                    request,
                    (
                        f"Workflow item {item.id} / "
                        f"Order {item.order.bon_de_commande} 同步失败：{exc}"
                    ),
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            f"同步完成：成功 {success}，失败 {failed}。",
            level=messages.SUCCESS if failed == 0 else messages.WARNING,
        )


    def validation_summary(self, obj):
        data = obj.validation_data or {}

        errors = data.get("errors", [])
        warnings = data.get("warnings", [])

        if not data:
            return "-"

        return f"E{len(errors)} / W{len(warnings)}"

    validation_summary.short_description = "校验摘要"

    @admin.action(description="校验所选待处理文件")
    def validate_selected_workflow_items(self, request, queryset):
        summary = validate_document_workflow_items(queryset)

        for result in summary["results"]:
            if "error" in result:
                self.message_user(
                    request,
                    f"Workflow item {result['item_id']} 校验失败：{result['error']}",
                    level=messages.ERROR,
                )
                continue

            status = result["status"]

            if status == "ready":
                level = messages.SUCCESS
            elif status == "blocked":
                level = messages.ERROR
            else:
                level = messages.WARNING

            self.message_user(
                request,
                (
                    f"Order {result['order']} / Batch {result['batch_number']}: "
                    f"{status}, errors={result['error_count']}, "
                    f"warnings={result['warning_count']}"
                ),
                level=level,
            )

        self.message_user(
            request,
            (
                f"校验完成：处理 {summary['processed']}，"
                f"ready={summary['ready']}，"
                f"needs_review={summary['needs_review']}，"
                f"blocked={summary['blocked']}，"
                f"failed={summary['failed']}。"
            ),
            level=messages.INFO,
        )

    @admin.action(description="重新应用价格策略")
    def apply_price_policy_to_selected_workflow_items(self, request, queryset):
        summary = apply_price_policy_to_workflow_items(queryset)

        for result in summary["results"]:
            bon = result["bon_de_commande"]

            if not result["success"]:
                self.message_user(
                    request,
                    (
                        f"Order {bon}: 价格策略应用失败："
                        + "; ".join(result["errors"])
                    ),
                    level=messages.ERROR,
                )
                continue

            msg = (
                f"Order {bon}: 已应用价格策略到 "
                f"{result['updated_count']} 个产品行。"
                f"日期={result['price_policy_date']}，"
                f"来源={result['date_source']}。"
                f"已重置 {result['reset_count']} 个待处理文件为未校验。"
            )

            if result["skipped_generated_count"]:
                msg += (
                    f" 已跳过 {result['skipped_generated_count']} 个已生成文件的任务。"
                )

            if result["warnings"]:
                msg += " Warnings: " + "; ".join(result["warnings"][:3])
                level = messages.WARNING
            else:
                level = messages.SUCCESS

            self.message_user(
                request,
                msg,
                level=level,
            )

        self.message_user(
            request,
            (
                f"价格策略完成：选中 {summary['selected_items']} 条待处理文件，"
                f"涉及 {summary['order_count']} 个订单，"
                f"成功 {summary['success_count']}，"
                f"失败 {summary['error_count']}，"
                f"重置待处理文件 {summary['reset_workflow_item_count']}。"
            ),
            level=messages.INFO,
        )

    @admin.action(description="生成 Invoice / Factory PO")
    def generate_documents_for_selected_workflow_items(self, request, queryset):
        summary = generate_documents_for_workflow_items(
            queryset=queryset,
            generated_by=request.user,
        )

        for result in summary["results"]:
            if not result["success"]:
                self.message_user(
                    request,
                    (
                        f"Workflow item {result['workflow_item_id']} / "
                        f"Order {result['bon_de_commande']} / "
                        f"Batch {result['batch_number']} 生成失败："
                        f"{result['error']}"
                    ),
                    level=messages.ERROR,
                )
                continue

            msg = (
                f"Order {result['bon_de_commande']} / "
                f"Batch {result['batch_number']}: "
                f"已生成 Invoice {result['invoice_number']} "
                f"和 PO {result['po_number']}。"
            )

            if result["warnings"]:
                msg += " Warnings: " + "; ".join(result["warnings"][:3])
                level = messages.WARNING
            else:
                level = messages.SUCCESS

            self.message_user(
                request,
                msg,
                level=level,
            )

        self.message_user(
            request,
            (
                f"生成完成：处理 {summary['processed']}，"
                f"成功 {summary['success']}，"
                f"失败 {summary['failed']}。"
            ),
            level=messages.INFO,
        )