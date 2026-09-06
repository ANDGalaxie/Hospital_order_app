from django.contrib import admin, messages
from django.utils.html import format_html
from .models import Order, OrderItem
from documents.services.factory_order_request_service import generate_factory_order_request

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0

    fields = (
        "product_match_badge",
        "product",
        "product_code",
        "requested_quantity",
        "confirmed_quantity",
        "backordered_quantity",
        "hospital_unit_price",
        "status",
        "product_match_status",
        "product_match_message",
        "is_manually_confirmed",
    )

    readonly_fields = (
        "product_match_badge",
        "created_at",
        "updated_at",
    )

    def product_match_badge(self, obj):
        if not obj or not obj.pk:
            return "-"

        if obj.product_match_status == OrderItem.ProductMatchStatus.OK:
            return format_html(
                '<strong style="color: green;">OK</strong>'
            )

        if obj.product_match_status == OrderItem.ProductMatchStatus.MANUALLY_CONFIRMED:
            return format_html(
                '<strong style="color: #1d4ed8;">MANUALLY CONFIRMED</strong>'
            )

        return format_html(
            '<strong style="color: red;">NEEDS REVIEW</strong>'
        )

    product_match_badge.short_description = "Product match"

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "bon_de_commande",
        "hospital_name",
        "hospital",
        "factory",
        "factory_match_status",
        "status",
        "extraction_status",
        "hospital_match_status",
        "document_validation_status",
        "created_by",
        "created_at",
    )

    list_filter = (
        "status",
        "extraction_status",
        "hospital_match_status",
        "factory",
        "factory_match_status",
        "document_validation_status",
        "created_at",
    )

    search_fields = (
        "bon_de_commande",
        "hospital_name",
        "hospital__name",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
        "extracted_at",
        "document_validation_data",
        "validated_at",
    )

    actions = [
        "extract_selected_hospital_orders",
        "validate_selected_orders_for_documents",
        "generate_documents_for_selected_orders",
        "generate_factory_order_request",
    ]

    fieldsets = (
        (
            "Basic order information",
            {
                "fields": (
                    "bon_de_commande",
                    "hospital_name",
                    "hospital",
                    "hospital_order_pdf",
                    "status",
                    "factory",
                    "factory_match_status",
                    "factory_match_message",
                    "created_by",
                    "notes",
                )
            },
        ),
        (
            "Hospital order extraction",
            {
                "fields": (
                    "extraction_status",
                    "extracted_order_data",
                    "confirmed_order_data",
                    "shipping_address_data",
                    "billing_address_data",
                    "extraction_error",
                    "extracted_at",
                )
            },
        ),
        (
            "Document generation validation",
            {
                "fields": (
                    "document_validation_status",
                    "document_validation_data",
                    "validated_at",
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

    inlines = [
        OrderItemInline,
    ]

    @admin.action(description="Extract selected hospital orders")
    def extract_selected_hospital_orders(self, request, queryset):
        """
        Admin action:
            选中一个或多个订单后，执行医院订单 OCR / extraction。
        """
        from orders.services.hospital_order_extraction_service import (
            extract_hospital_order_for_order,
        )

        success_count = 0
        failed_count = 0

        for order in queryset:
            try:
                extract_hospital_order_for_order(
                    order=order,
                    force_ocr=False,
                )

                success_count += 1

                self.message_user(
                    request,
                    f"Order {order.bon_de_commande}: hospital order extracted successfully.",
                    level=messages.SUCCESS,
                )

            except Exception as exc:
                failed_count += 1

                self.message_user(
                    request,
                    f"Order {order.bon_de_commande}: extraction failed: {exc}",
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            f"Extraction finished. Success: {success_count}, Failed: {failed_count}.",
            level=messages.INFO,
        )
        
    @admin.action(description="Validate selected orders for document generation")
    def validate_selected_orders_for_documents(self, request, queryset):
        from orders.services.order_validation_service import (
            validate_order_for_document_generation,
        )

        success_count = 0
        blocked_count = 0

        for order in queryset:
            result = validate_order_for_document_generation(
                order=order,
                save=True,
            )

            if result["can_generate_documents"]:
                success_count += 1

                self.message_user(
                    request,
                    f"Order {order.bon_de_commande}: ready for document generation.",
                    level=messages.SUCCESS,
                )
            else:
                blocked_count += 1

                error_text = "; ".join(result["errors"][:3])

                self.message_user(
                    request,
                    (
                        f"Order {order.bon_de_commande}: blocked. "
                        f"{error_text}"
                    ),
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            (
                f"Validation finished. "
                f"Ready: {success_count}, Blocked: {blocked_count}."
            ),
            level=messages.INFO,
        )
        
    @admin.action(description="Generate invoice and factory PO")
    def generate_documents_for_selected_orders(self, request, queryset):
        from documents.services.document_generation_service import (
            generate_all_documents_for_order,
        )

        success_count = 0
        failed_count = 0

        for order in queryset:
            try:
                result = generate_all_documents_for_order(
                    order=order,
                    generated_by=request.user,
                    document_date=None,
                )

                success_count += 1

                invoice_number = result["invoice"]["document_number"]
                po_number = result["factory_po"]["document_number"]

                self.message_user(
                    request,
                    (
                        f"Order {order.bon_de_commande}: documents generated. "
                        f"Invoice={invoice_number}, PO={po_number}"
                    ),
                    level=messages.SUCCESS,
                )

            except Exception as exc:
                failed_count += 1

                self.message_user(
                    request,
                    (
                        f"Order {order.bon_de_commande}: document generation failed: "
                        f"{exc}"
                    ),
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            (
                f"Document generation finished. "
                f"Success: {success_count}, Failed: {failed_count}."
            ),
            level=messages.INFO,
        )

    @admin.action(description="Generate Factory Order Request from hospital order")
    def generate_factory_order_request(self, request, queryset):
        success_count = 0
        error_count = 0

        for order in queryset:
            try:
                generate_factory_order_request(
                    order=order,
                    generated_by=request.user,
                )
                success_count += 1

            except Exception as exc:
                error_count += 1
                self.message_user(
                    request,
                    f"Order {order.bon_de_commande}: {exc}",
                    level=messages.ERROR,
                )

        if success_count:
            self.message_user(
                request,
                f"Generated {success_count} Factory Order Request document(s).",
                level=messages.SUCCESS,
            )

        if error_count:
            self.message_user(
                request,
                f"{error_count} document(s) failed.",
                level=messages.WARNING,
            )