from django.contrib import admin

from .models import Hospital


@admin.register(Hospital)
class HospitalAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "phone",
        "email",
        "is_active",
        "updated_at",
    )

    search_fields = (
        "name",
        "normalized_name",
        "billing_address",
        "default_shipping_address",
    )

    list_filter = (
        "is_active",
    )