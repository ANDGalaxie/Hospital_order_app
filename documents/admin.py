from django.contrib import admin
from django.utils.html import format_html

from .models import (
    DocumentSequence,
    GeneratedDocument,
    HospitalInvoiceDocument,
    FactoryPurchaseOrderDocument,
)


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = (
        "month_key",
        "bon_de_commande",
        "sequence",
        "invoice_number",
        "po_number",
        "created_at",
    )

    list_filter = (
        "month_key",
    )

    search_fields = (
        "bon_de_commande",
        "invoice_number",
        "po_number",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
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