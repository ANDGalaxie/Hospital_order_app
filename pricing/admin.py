from django.contrib import admin

from pricing.models import PricePolicy


@admin.register(PricePolicy)
class PricePolicyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "factory",
        "category",
        "start_date",
        "end_date",
        "hospital_unit_price",
        "factory_unit_price",
        "expiration_discount_rate",
        "is_active",
    )

    list_filter = (
        "factory",
        "category",
        "is_active",
        "start_date",
        "end_date",
    )

    search_fields = (
        "name",
        "factory__name",
        "factory__short_name",
        "category__name",
        "notes",
    )

    fields = (
        "name",
        "factory",
        "category",
        "start_date",
        "end_date",
        "hospital_unit_price",
        "factory_unit_price",
        "expiration_discount_rate",
        "is_active",
        "notes",
    )

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)