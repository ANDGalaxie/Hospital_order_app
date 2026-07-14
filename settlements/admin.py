from django.contrib import admin

from settlements.models import (
    PaymentTransaction,
    SettlementAccount,
)


@admin.register(SettlementAccount)
class SettlementAccountAdmin(
    admin.ModelAdmin
):
    list_display = [
        "document_number",
        "direction",
        "counterparty_name",
        "original_amount",
        "posted_amount_display",
        "remaining_amount_display",
        "status",
        "issue_date",
        "due_date",
    ]

    list_filter = [
        "direction",
        "status",
        "currency",
        "issue_date",
        "due_date",
    ]

    search_fields = [
        "document__document_number",
        "document__order__bon_de_commande",
        "counterparty_name",
        "notes",
    ]

    readonly_fields = [
        "direction",
        "posted_amount_display",
        "remaining_amount_display",
        "created_at",
        "updated_at",
        "cancelled_at",
        "cancelled_by",
    ]

    raw_id_fields = [
        "document",
    ]

    fieldsets = [
        (
            "正式文档",
            {
                "fields": [
                    "document",
                    "direction",
                    "counterparty_name",
                ],
            },
        ),
        (
            "结算信息",
            {
                "fields": [
                    "issue_date",
                    "due_date",
                    "currency",
                    "original_amount",
                    "posted_amount_display",
                    "remaining_amount_display",
                    "status",
                    "notes",
                ],
            },
        ),
        (
            "取消记录",
            {
                "fields": [
                    "cancellation_reason",
                    "cancelled_at",
                    "cancelled_by",
                ],
            },
        ),
        (
            "系统信息",
            {
                "fields": [
                    "created_at",
                    "updated_at",
                ],
            },
        ),
    ]

    @admin.display(
        description="文档编号",
        ordering="document__document_number",
    )
    def document_number(self, obj):
        return obj.document.document_number

    @admin.display(
        description="已收 / 已付",
    )
    def posted_amount_display(
        self,
        obj,
    ):
        return obj.posted_amount

    @admin.display(
        description="剩余金额",
    )
    def remaining_amount_display(
        self,
        obj,
    ):
        return obj.remaining_amount

    def has_add_permission(
        self,
        request,
    ):
        # 下一阶段由同步服务自动创建。
        return False

    def has_delete_permission(
        self,
        request,
        obj=None,
    ):
        return False


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(
    admin.ModelAdmin
):
    list_display = [
        "payment_date",
        "direction_display",
        "document_number",
        "amount",
        "method",
        "reference",
        "status",
        "created_by",
    ]

    list_filter = [
        "status",
        "method",
        "payment_date",
        "account__direction",
    ]

    search_fields = [
        "account__document__document_number",
        "account__document__order__bon_de_commande",
        "account__counterparty_name",
        "reference",
        "notes",
    ]

    autocomplete_fields = [
        "account",
    ]

    readonly_fields = [
        "status",
        "created_by",
        "created_at",
        "updated_at",
        "reversed_at",
        "reversed_by",
        "reversal_reason",
    ]

    @admin.display(
        description="方向",
        ordering="account__direction",
    )
    def direction_display(self, obj):
        return obj.account.get_direction_display()

    @admin.display(
        description="文档编号",
        ordering=(
            "account__document__document_number"
        ),
    )
    def document_number(self, obj):
        return (
            obj.account.document.document_number
        )

    def save_model(
        self,
        request,
        obj,
        form,
        change,
    ):
        if not obj.created_by_id:
            obj.created_by = request.user

        super().save_model(
            request,
            obj,
            form,
            change,
        )

    def has_delete_permission(
        self,
        request,
        obj=None,
    ):
        return False
