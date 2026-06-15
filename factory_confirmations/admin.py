from django.contrib import admin, messages
from django.utils import timezone
from .models import FactoryConfirmation, SerialItem


class SerialItemInline(admin.TabularInline):
    model = SerialItem
    extra = 0
    readonly_fields = (
        "created_at",
    )


@admin.register(FactoryConfirmation)
class FactoryConfirmationAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "extraction_status",
        "shipping_date",
        "created_by",
        "created_at",
        "bon_de_commande_manual_confirmed",
        "bon_de_commande_manual_confirmed_by",
        "bon_de_commande_manual_confirmed_at",
    )

    list_filter = (
        "extraction_status",
        "shipping_date",
        "created_at",
    )

    search_fields = (
        "order__bon_de_commande",
    )

    readonly_fields = (
        "extracted_at",
        "created_at",
        "updated_at",
    )

    actions = [
        "extract_selected_factory_confirmations",
        "manually_confirm_bon_de_commande_match",
    ]

    fieldsets = (
        (
            "Basic information",
            {
                "fields": (
                    "order",
                    "confirmation_pdf",
                    "shipping_date",
                    "created_by",
                    "notes",
                )
            },
        ),
        (
            "Extraction",
            {
                "fields": (
                    "extraction_status",
                    "extracted_confirmation_data",
                    "extraction_error",
                    "extracted_at",
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
        SerialItemInline,
    ]

    @admin.action(description="Extract selected factory confirmations")
    def extract_selected_factory_confirmations(self, request, queryset):
        """
        Admin action:
            选中一个或多个工厂确认文件后，执行 serial number / expiration date 提取。
        """
        from factory_confirmations.services.factory_confirmation_extraction_service import (
            extract_factory_confirmation_for_confirmation,
        )

        success_count = 0
        failed_count = 0

        for confirmation in queryset:
            try:
                extract_factory_confirmation_for_confirmation(
                    confirmation=confirmation,
                )

                success_count += 1

                self.message_user(
                    request,
                    (
                        f"Factory confirmation for order "
                        f"{confirmation.order.bon_de_commande}: extracted successfully."
                    ),
                    level=messages.SUCCESS,
                )

            except Exception as exc:
                failed_count += 1

                self.message_user(
                    request,
                    (
                        f"Factory confirmation for order "
                        f"{confirmation.order.bon_de_commande}: extraction failed: {exc}"
                    ),
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            f"Factory extraction finished. Success: {success_count}, Failed: {failed_count}.",
            level=messages.INFO,
        )
    @admin.action(description="Manually confirm selected confirmations belong to their linked orders")
    def manually_confirm_bon_de_commande_match(self, request, queryset):
        count = 0

        for confirmation in queryset:
            confirmation.bon_de_commande_manual_confirmed = True
            confirmation.bon_de_commande_manual_confirmed_by = request.user
            confirmation.bon_de_commande_manual_confirmed_at = timezone.now()

            if not confirmation.bon_de_commande_manual_note:
                confirmation.bon_de_commande_manual_note = (
                    "Manually confirmed in Django admin."
                )

            confirmation.save(update_fields=[
                "bon_de_commande_manual_confirmed",
                "bon_de_commande_manual_confirmed_by",
                "bon_de_commande_manual_confirmed_at",
                "bon_de_commande_manual_note",
                "updated_at",
            ])

            count += 1

        self.message_user(
            request,
            f"{count} factory confirmation(s) manually confirmed.",
            level=messages.SUCCESS,
        )


@admin.register(SerialItem)
class SerialItemAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "product_code",
        "serial_number",
        "expiration_date",
        "discount_rate",
    )

    search_fields = (
        "order__bon_de_commande",
        "product_code",
        "serial_number",
    )

    list_filter = (
        "expiration_date",
        "discount_rate",
    )