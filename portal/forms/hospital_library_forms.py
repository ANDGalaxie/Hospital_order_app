import re
import unicodedata

from django import forms

from hospitals.models import Hospital


def normalize_hospital_name(value):
    """
    将医院名称转换为适合匹配的标准化文本。

    处理内容：
    - 去除法语重音符号
    - 转成小写
    - 标点符号替换为空格
    - 合并连续空格
    """
    text = str(value or "").strip()

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        char
        for char in text
        if not unicodedata.combining(char)
    )

    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


class HospitalPortalForm(forms.ModelForm):
    class Meta:
        model = Hospital
        fields = (
            "name",
            "billing_address",
            "default_shipping_address",
            "contact_name",
            "phone",
            "fax",
            "email",
            "notes",
            "is_active",
        )

        labels = {
            "name": "医院正式名称",
            "billing_address": "账单地址",
            "default_shipping_address": "默认收货地址",
            "contact_name": "联系人",
            "phone": "电话",
            "fax": "传真",
            "email": "邮箱",
            "notes": "备注",
            "is_active": "启用",
        }

        widgets = {
            "billing_address": forms.Textarea(
                attrs={"rows": 4}
            ),
            "default_shipping_address": forms.Textarea(
                attrs={"rows": 4}
            ),
            "notes": forms.Textarea(
                attrs={"rows": 4}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            widget = field.widget

            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault(
                    "class",
                    "master-checkbox",
                )
            else:
                widget.attrs.setdefault(
                    "class",
                    "master-form-control",
                )

        if not self.is_bound and not self.instance.pk:
            self.fields["is_active"].initial = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        duplicate_query = Hospital.objects.filter(
            name__iexact=name,
        )

        if self.instance.pk:
            duplicate_query = duplicate_query.exclude(
                pk=self.instance.pk,
            )

        if duplicate_query.exists():
            raise forms.ValidationError(
                "已经存在同名医院。"
            )

        return name

    def clean_email(self):
        return (
            self.cleaned_data.get("email")
            or ""
        ).strip()

    def save(self, commit=True):
        hospital = super().save(commit=False)

        hospital.normalized_name = normalize_hospital_name(
            hospital.name
        )

        if commit:
            hospital.save()

        return hospital
