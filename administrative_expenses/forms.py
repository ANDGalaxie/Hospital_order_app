from django import forms
from django.utils.translation import gettext_lazy as _

from .categories import SUBCATEGORIES
from .models import AdministrativeExpense, AdministrativeExpenseAttachment
from .validation import validate_uploads


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        uploads = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        clean = super().clean
        return validate_uploads([clean(file, initial) for file in uploads])


class AttachmentForm(forms.Form):
    files = MultipleFileField(required=False, widget=MultipleFileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"}), label=_("Attachments / Justificatifs"))
    document_type = forms.ChoiceField(choices=AdministrativeExpenseAttachment.DocumentType.choices, initial="other", label=_("Document type"))


class ExpenseForm(forms.ModelForm):
    subcategory = forms.ChoiceField(required=False, choices=[("", _("No subcategory"))] + [
        (code, label) for group in SUBCATEGORIES.values() for code, label in group.items()
    ], label=_("Subcategory"))

    class Meta:
        model = AdministrativeExpense
        fields = ["category", "subcategory", "expense_date", "employee_or_payee", "amount", "description", "payment_status", "paid_at"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "paid_at": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 3}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
        }
