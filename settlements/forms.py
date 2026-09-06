from decimal import Decimal

from django import forms
from django.utils import timezone

from settlements.models import PaymentTransaction


class PaymentEntryForm(forms.Form):
    payment_date = forms.DateField(
        label="收付款日期",
        initial=timezone.localdate,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "type": "date",
            },
        ),
    )

    amount = forms.DecimalField(
        label="金额",
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )

    method = forms.ChoiceField(
        label="收付款方式",
        choices=PaymentTransaction.Method.choices,
        initial=PaymentTransaction.Method.BANK_TRANSFER,
    )

    reference = forms.CharField(
        label="银行参考号",
        max_length=200,
        required=False,
    )

    notes = forms.CharField(
        label="备注",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
            },
        ),
    )

    def __init__(
        self,
        *args,
        account=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.account = account

        if account is not None:
            self.fields["amount"].widget.attrs[
                "max"
            ] = str(account.remaining_amount)

            self.fields["amount"].help_text = (
                f"当前剩余金额："
                f"{account.currency} "
                f"{account.remaining_amount}"
            )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]

        if (
            self.account is not None
            and amount
            > self.account.remaining_amount
        ):
            raise forms.ValidationError(
                "本次金额不能超过当前剩余金额。"
            )

        return amount


class SettlementDueDateForm(forms.Form):
    due_date = forms.DateField(
        label="付款截止日期",
        required=False,
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "type": "date",
            },
        ),
    )

    def __init__(
        self,
        *args,
        account=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.account = account

        if (
            account is not None
            and not self.is_bound
        ):
            self.fields["due_date"].initial = (
                account.due_date
            )

    def clean_due_date(self):
        due_date = self.cleaned_data.get(
            "due_date"
        )

        if (
            due_date
            and self.account is not None
            and due_date
            < self.account.issue_date
        ):
            raise forms.ValidationError(
                "付款截止日期不能早于开立日期。"
            )

        return due_date


class PaymentReversalForm(forms.Form):
    reversal_reason = forms.CharField(
        label="冲销原因",
        max_length=500,
        required=True,
    )
