from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html
from datetime import datetime
from django.db.models import Q

from .models import (
    DocumentSequence,
    GeneratedDocument,
    HospitalInvoiceDocument,
    FactoryPurchaseOrderDocument,
    FactoryOrderRequestDocument,
)


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = (
        "month_key",
        "bon_de_commande",
        "sequence",
        "invoice_number",
        "po_number",
        "payment_status_badge",
        "payment_due_date",
        "payment_reminder_date",
        "payment_reminder_badge",
        "paid_at",
        "created_at",
    )

    list_filter = (
        "month_key",
        "payment_status",
        "payment_due_date",
        "payment_reminder_date",
    )

    search_fields = (
        "bon_de_commande",
        "invoice_number",
        "po_number",
    )

    readonly_fields = (
        "payment_status_badge",
        "payment_reminder_date",
        "payment_reminder_badge",
        "created_at",
        "updated_at",
    )   

    fields = (
        "month_key",
        "bon_de_commande",
        "sequence",
        "invoice_number",
        "po_number",
        "payment_status",
        "payment_status_badge",
        "payment_due_date",
        "payment_reminder_date",
        "payment_reminder_badge",
        "paid_at",
        "payment_notes",
        "created_at",
        "updated_at",
    )

    actions = (
        "sync_payment_due_date_from_invoices",
        "mark_selected_as_paid",
        "mark_selected_as_unpaid",
    )

    def payment_status_badge(self, obj):
        if obj.payment_status == DocumentSequence.PaymentStatus.PAID:
            return format_html(
                '<strong style="color: green;">✓ 已付款</strong>'
            )

        return format_html(
            '<strong style="color: red;">✕ 未付款</strong>'
        )

    payment_status_badge.short_description = "付款状态"

    def save_model(self, request, obj, form, change):
        if obj.payment_status == DocumentSequence.PaymentStatus.PAID:
            if obj.paid_at is None:
                obj.paid_at = timezone.now()

        if obj.payment_status == DocumentSequence.PaymentStatus.UNPAID:
            obj.paid_at = None

        super().save_model(request, obj, form, change)

    @admin.action(description="标记为已付款")
    def mark_selected_as_paid(self, request, queryset):
        now = timezone.now()

        updated = queryset.update(
            payment_status=DocumentSequence.PaymentStatus.PAID,
            paid_at=now,
            updated_at=now,
        )

        self.message_user(
            request,
            f"已将 {updated} 条 sequence 标记为已付款。",
            level=messages.SUCCESS,
        )

    @admin.action(description="标记为未付款")
    def mark_selected_as_unpaid(self, request, queryset):
        now = timezone.now()

        updated = queryset.update(
            payment_status=DocumentSequence.PaymentStatus.UNPAID,
            paid_at=None,
            updated_at=now,
        )

        self.message_user(
            request,
            f"已将 {updated} 条 sequence 标记为未付款。",
            level=messages.WARNING,
        )

    def payment_reminder_badge(self, obj):
        if obj.payment_status == DocumentSequence.PaymentStatus.PAID:
            return format_html(
                '<strong style="color: green;">✓ 已付款</strong>'
            )

        if not obj.payment_due_date:
            return format_html(
                '<span style="color: gray;">未设置截止日期</span>'
            )

        today = timezone.localdate()

        if obj.payment_due_date < today:
            return format_html(
                '<strong style="color: red;">已逾期</strong>'
            )

        if obj.payment_reminder_date and obj.payment_reminder_date <= today:
            return format_html(
                '<strong style="color: orange;">需要提醒</strong>'
            )

        return format_html(
            '<span style="color: green;">未到提醒日</span>'
        )

    payment_reminder_badge.short_description = "提醒状态"

    def parse_invoice_due_date(self, document):
        source_data = document.source_data or {}
        invoice_data = source_data.get("invoice_data") or {}
        invoice = invoice_data.get("invoice") or {}

        raw_due_date = invoice.get("due_date")

        if not raw_due_date:
            return None

        text = str(raw_due_date).strip()

        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass

        return None

    @admin.action(description="从已生成 Invoice 同步付款截止日期")
    def sync_payment_due_date_from_invoices(self, request, queryset):
        success_count = 0
        missing_count = 0
        failed_count = 0

        for sequence in queryset:
            try:
                invoice_qs = GeneratedDocument.objects.filter(
                    document_type=GeneratedDocument.DocumentType.HOSPITAL_INVOICE,
                    order__bon_de_commande=sequence.bon_de_commande,
                ).filter(
                    Q(document_number=sequence.invoice_number)
                    | Q(document_number__startswith=f"{sequence.invoice_number}-B")
                ).order_by("generated_at")

                due_dates = []

                for document in invoice_qs:
                    due_date = self.parse_invoice_due_date(document)

                    if due_date:
                        due_dates.append(due_date)

                if not due_dates:
                    missing_count += 1
                    continue

                sequence.payment_due_date = min(due_dates)
                sequence.save(
                    update_fields=[
                        "payment_due_date",
                        "payment_reminder_date",
                        "updated_at",
                    ]
                )

                success_count += 1

            except Exception as exc:
                failed_count += 1
                self.message_user(
                    request,
                    (
                        f"Sequence {sequence.bon_de_commande} 同步失败：{exc}"
                    ),
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            (
                f"付款截止日期同步完成：成功 {success_count}，"
                f"未找到 due date {missing_count}，失败 {failed_count}。"
            ),
            level=messages.SUCCESS if failed_count == 0 else messages.WARNING,
        )


class BaseGeneratedDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "document_number",
        "pdf_link",
        "html_link",
        "generated_by",
        "generated_at",
    )

    list_filter = (
        "generated_at",
    )

    search_fields = (
        "order__bon_de_commande",
        "document_number",
    )

    readonly_fields = (
        "order",
        "document_type",
        "document_number",
        "pdf_file",
        "html_file",
        "source_data",
        "generated_by",
        "generated_at",
        "pdf_link",
        "html_link",
    )

    def has_add_permission(self, request):
        return False

    def pdf_link(self, obj):
        if obj.pdf_file:
            return format_html(
                '<a href="{}" target="_blank">Open PDF</a>',
                obj.pdf_file.url,
            )
        return "-"

    pdf_link.short_description = "PDF"

    def html_link(self, obj):
        if obj.html_file:
            return format_html(
                '<a href="{}" target="_blank">Open HTML</a>',
                obj.html_file.url,
            )
        return "-"

    html_link.short_description = "HTML"


@admin.register(HospitalInvoiceDocument)
class HospitalInvoiceDocumentAdmin(BaseGeneratedDocumentAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document_type="hospital_invoice")


@admin.register(FactoryPurchaseOrderDocument)
class FactoryPurchaseOrderDocumentAdmin(BaseGeneratedDocumentAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document_type="factory_po")


@admin.register(FactoryOrderRequestDocument)
class FactoryOrderRequestDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "document_number",
        "order",
        "generated_by",
        "generated_at",
        "pdf_link",
        "html_link",
    )
    search_fields = (
        "document_number",
        "order__bon_de_commande",
    )
    readonly_fields = (
        "order",
        "document_type",
        "document_number",
        "pdf_file",
        "html_file",
        "source_data",
        "generated_by",
        "generated_at",
        "notes",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(document_type="factory_order_request")

    def pdf_link(self, obj):
        if obj.pdf_file:
            from django.utils.html import format_html
            return format_html('<a href="{}" target="_blank">Open PDF</a>', obj.pdf_file.url)
        return "-"

    pdf_link.short_description = "PDF"

    def html_link(self, obj):
        if obj.html_file:
            from django.utils.html import format_html
            return format_html('<a href="{}" target="_blank">Open HTML</a>', obj.html_file.url)
        return "-"

    html_link.short_description = "HTML"

    def has_add_permission(self, request):
        return False

from config.admin_sidebar import patch_admin_sidebar

patch_admin_sidebar()