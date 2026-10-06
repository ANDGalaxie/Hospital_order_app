from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from hospital_engagements.models import (
    HospitalContact, HospitalDepartment, HospitalEngagement,
    HospitalFollowUp, HospitalProductInterest,
)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3


def departments_for(hospital_id, existing_id=None):
    return HospitalDepartment.objects.filter(hospital_id=hospital_id).filter(
        Q(is_active=True) | Q(pk=existing_id)
    )


class BusinessForm(StyledModelForm):
    class Meta:
        model = HospitalEngagement
        fields = ("priority", "owner", "demand_summary", "special_requirements",
                  "next_action", "next_follow_up_date")
        widgets = {"next_follow_up_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")


class DepartmentForm(StyledModelForm):
    class Meta:
        model = HospitalDepartment
        fields = ("name", "notes", "is_active")

    def __init__(self, *args, hospital, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.hospital = hospital

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_active") and cleaned.get("name"):
            duplicates = HospitalDepartment.objects.filter(
                hospital=self.instance.hospital, is_active=True, name__iexact=cleaned["name"]
            ).exclude(pk=self.instance.pk)
            if duplicates.exists():
                self.add_error("name", _("该医院已有同名启用科室。"))
        return cleaned


class ContactForm(StyledModelForm):
    class Meta:
        model = HospitalContact
        fields = ("name", "contact_type", "department", "title", "email", "phone",
                  "is_primary", "notes", "is_active")

    def __init__(self, *args, hospital, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.hospital = hospital
        self.fields["department"].queryset = departments_for(hospital.pk, self.instance.department_id)


class InterestForm(StyledModelForm):
    class Meta:
        model = HospitalProductInterest
        fields = ("department", "product", "product_text", "notes")

    def __init__(self, *args, engagement, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.engagement = engagement
        self.fields["department"].queryset = departments_for(engagement.hospital_id, self.instance.department_id)


class CommunicationForm(StyledModelForm):
    class Meta:
        model = HospitalFollowUp
        fields = ("occurred_at", "channel", "contact", "department", "summary",
                  "outcome", "next_action", "next_follow_up_date")
        widgets = {
            "occurred_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "next_follow_up_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, engagement, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.engagement = engagement
        self.fields["contact"].queryset = HospitalContact.objects.filter(
            hospital_id=engagement.hospital_id, is_active=True
        )
        self.fields["department"].queryset = departments_for(engagement.hospital_id)
        self.fields["channel"].required = True
        self.fields["summary"].required = True


class StageForm(forms.Form):
    stage = forms.ChoiceField(label=_("阶段"), choices=HospitalEngagement.Stage.choices)


class BulkStageForm(StageForm):
    engagements = forms.ModelMultipleChoiceField(label=_("医院"), queryset=HospitalEngagement.objects.none())

    def __init__(self, *args, queryset, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["engagements"].queryset = queryset


class FilterForm(forms.Form):
    q = forms.CharField(label=_("搜索医院、联系人或产品需求"), required=False, max_length=255)
    owner = forms.ModelChoiceField(label=_("负责人"), queryset=get_user_model().objects.none(),
                                  required=False, empty_label=_("全部"))
    priority = forms.ChoiceField(label=_("优先级"), required=False,
                                choices=[("", _("全部")), *HospitalEngagement.Priority.choices])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
