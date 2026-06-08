from django.contrib import admin

from .models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """
    Order 在 Django Admin 后台中的显示方式。
    """

    list_display = (
        "bon_de_commande",
        "hospital_name",
        "status",
        "created_by",
        "created_at",
    )

    list_filter = (
        "status",
        "created_at",
    )

    search_fields = (
        "bon_de_commande",
        "hospital_name",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

# Register your models here.
