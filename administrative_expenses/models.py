from decimal import Decimal
from pathlib import PurePosixPath
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .categories import CATEGORIES, SUBCATEGORIES, category_label, validate_category
from .storage import private_storage


def attachment_path(instance, filename):
    return f"administrative_expenses/{instance.expense_id}/{uuid4().hex}{PurePosixPath(filename).suffix.lower()}"


class AdministrativeExpense(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        PAID = "paid", _("Paid")

    category = models.CharField(max_length=32, choices=list(CATEGORIES.items()), verbose_name=_("Category"))
    subcategory = models.CharField(max_length=32, blank=True, default="", verbose_name=_("Subcategory"))
    expense_date = models.DateField(verbose_name=_("Expense date"))
    employee_or_payee = models.CharField(max_length=200, blank=True, verbose_name=_("Employee / payee"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))], verbose_name=_("Amount (EUR)"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    payment_status = models.CharField(max_length=8, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, verbose_name=_("Payment status"))
    paid_at = models.DateField(null=True, blank=True, verbose_name=_("Payment date"))
    is_void = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="administrative_expenses_created")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="administrative_expenses_updated")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expense_date", "-pk"]
        default_permissions = ()
        indexes = [models.Index(fields=["expense_date", "is_void"], name="admin_expense_month_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="admin_expense_positive"),
            models.CheckConstraint(condition=(
                Q(category__in=["salary", "office_supplies", "premises"], subcategory="")
                | Q(category="benefits", subcategory__in=list(SUBCATEGORIES["benefits"]))
                | Q(category="reimbursement", subcategory__in=list(SUBCATEGORIES["reimbursement"]))
            ), name="admin_expense_category"),
            models.CheckConstraint(condition=(
                Q(payment_status="pending", paid_at__isnull=True)
                | Q(payment_status="paid", paid_at__isnull=False)
            ), name="admin_expense_payment"),
            models.CheckConstraint(condition=Q(paid_at__isnull=True) | Q(paid_at__gte=models.F("expense_date")), name="admin_expense_paid_date"),
        ]

    def clean(self):
        super().clean()
        validate_category(self.category, self.subcategory)
        errors = {}
        if self.category in {"salary", "benefits", "reimbursement"} and not self.employee_or_payee.strip():
            errors["employee_or_payee"] = _("Employee / payee is required for this category.")
        if self.payment_status == "paid" and not self.paid_at:
            errors["paid_at"] = _("Paid expenses require a payment date.")
        if self.payment_status == "pending" and self.paid_at:
            errors["paid_at"] = _("Pending expenses cannot have a payment date.")
        if self.paid_at and (self.paid_at > timezone.localdate() or (self.expense_date and self.paid_at < self.expense_date)):
            errors["paid_at"] = _("Payment date must be on or after the expense date and cannot be in the future.")
        if errors:
            raise ValidationError(errors)

    @property
    def category_display(self):
        return category_label(self.category, self.subcategory)

    @property
    def has_supporting_document(self):
        count = getattr(self, "attachment_count", None)
        return count > 0 if count is not None else self.attachments.filter(removed_at__isnull=True).exists()

    @property
    def supporting_document_status(self):
        return _("Supporting document attached") if self.has_supporting_document else _("Missing supporting document")


class AdministrativeExpenseAttachment(models.Model):
    class DocumentType(models.TextChoices):
        INVOICE = "invoice", _("Invoice")
        RECEIPT = "receipt", _("Receipt")
        PAYSLIP = "payslip", _("Payslip")
        CONTRACT = "contract", _("Contract")
        PAYMENT_PROOF = "payment_proof", _("Payment proof")
        OTHER = "other", _("Other")

    expense = models.ForeignKey(AdministrativeExpense, on_delete=models.PROTECT, related_name="attachments")
    file = models.FileField(storage=private_storage, upload_to=attachment_path, max_length=200, unique=True)
    original_filename = models.CharField(max_length=255)
    document_type = models.CharField(max_length=20, choices=DocumentType.choices, default=DocumentType.OTHER)
    file_size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="administrative_expense_uploads")
    created_at = models.DateTimeField(auto_now_add=True)
    # A tombstone retains ownership until storage deletion succeeds; never counted as evidence.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        default_permissions = ()
