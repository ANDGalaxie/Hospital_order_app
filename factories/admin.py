from django.contrib import admin

from .models import Factory


@admin.register(Factory)
class FactoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "short_name",
        "buyer",
        "is_active",
        "created_at",
    )

    list_filter = (
        "is_active",
    )

    search_fields = (
        "name",
        "short_name",
        "buyer",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )