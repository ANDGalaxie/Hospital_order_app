from django.contrib import admin

from .models import CommercialPOPricePolicy


@admin.register(CommercialPOPricePolicy)
class CommercialPOPricePolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "unit_price", "currency", "is_active")
    readonly_fields = ("created_at", "updated_at")
