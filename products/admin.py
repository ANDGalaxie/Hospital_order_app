from django.contrib import admin

from .models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "hospital_unit_price",
        "factory_unit_price",
        "is_active",
    )

    search_fields = (
        "code",
        "description",
    )

    list_filter = (
        "is_active",
    )