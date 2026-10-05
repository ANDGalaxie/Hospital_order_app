from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django import forms

from factories.models import Factory


class FactoryPortalForm(forms.ModelForm):
    """
    工厂库新增和编辑共用表单。

    第一版只使用 Factory 模型中已经存在的字段，
    不修改数据库结构。
    """

    class Meta:
        model = Factory

        fields = (
            "name",
            "legal_name",
            "short_name",
            "address",
            "buyer",
            "default_product_description",
            "match_keywords",
            "notes",
            "is_active",
        )

        labels = {
            "name": _("工厂正式名称"),
            "legal_name": _("完整法律名称"),
            "short_name": _("工厂简称"),
            "address": _("工厂地址"),
            "buyer": _("采购联系人 / Buyer"),
            "default_product_description": _("默认产品描述"),
            "match_keywords": _("自动匹配关键词"),
            "notes": _("备注"),
            "is_active": _("启用"),
        }

        help_texts = {
            "short_name": (
                _("产品库卡片和日常页面优先显示此简称。")
            ),
            "default_product_description": (
                _("生成 Factory Purchase Order 时使用的默认描述。")
            ),
            "match_keywords": (
                _("每行填写一个关键词，例如简称、英文名、地址关键词。")
            ),
        }

        widgets = {
            "address": forms.Textarea(
                attrs={"rows": 4}
            ),
            "match_keywords": forms.Textarea(
                attrs={"rows": 5}
            ),
            "notes": forms.Textarea(
                attrs={"rows": 4}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.help_text = gettext(str(field.help_text))
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
        name = (
            self.cleaned_data.get("name")
            or ""
        ).strip()

        duplicate_query = Factory.objects.filter(
            name__iexact=name,
        )

        if self.instance.pk:
            duplicate_query = duplicate_query.exclude(
                pk=self.instance.pk,
            )

        if duplicate_query.exists():
            raise forms.ValidationError(
                _("工厂库中已经存在相同正式名称的工厂。")
            )

        return name

    def clean_short_name(self):
        short_name = (
            self.cleaned_data.get("short_name")
            or ""
        ).strip()

        if not short_name:
            return ""

        duplicate_query = Factory.objects.filter(
            short_name__iexact=short_name,
        )

        if self.instance.pk:
            duplicate_query = duplicate_query.exclude(
                pk=self.instance.pk,
            )

        if duplicate_query.exists():
            raise forms.ValidationError(
                _("工厂库中已经存在相同简称的工厂。")
            )

        return short_name

    def save(self, commit=True):
        factory = super().save(commit=False)

        text_fields = (
            "name",
            "legal_name",
            "short_name",
            "address",
            "buyer",
            "default_product_description",
            "match_keywords",
            "notes",
        )

        for field_name in text_fields:
            value = getattr(factory, field_name, "")
            setattr(
                factory,
                field_name,
                str(value or "").strip(),
            )

        if commit:
            factory.save()

        return factory
