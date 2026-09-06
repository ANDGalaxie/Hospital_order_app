from decimal import Decimal

from django import forms
from django.db.models import Q

from factories.models import Factory
from pricing.models import PricePolicy
from products.models import Product, ProductCategory


class ProductCategoryChoiceField(
    forms.ModelChoiceField
):
    def label_from_instance(self, obj):
        return obj.get_full_path()


class ProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        description = (
            str(obj.description or "")
            .strip()
            .replace("\n", " ")
        )

        if len(description) > 70:
            description = description[:67] + "..."

        if description:
            return f"{obj.code} — {description}"

        return obj.code


class PricePolicyPortalForm(forms.ModelForm):
    """
    价格规则新增和编辑表单。

    页面输入折扣百分比：
        30

    数据库存储：
        0.3000
    """

    expiration_discount_percent = forms.DecimalField(
        label="临期折扣率（%）",
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        decimal_places=2,
        max_digits=6,
        initial=Decimal("30"),
        help_text=(
            "输入 30 表示优惠 30%，"
            "即工厂最终按原采购价的 70% 结算。"
        ),
    )

    category = ProductCategoryChoiceField(
        label="产品分类",
        queryset=ProductCategory.objects.none(),
        required=False,
        empty_label="全部产品分类 / 工厂通用规则",
        help_text=(
            "留空表示适用于所选工厂的全部产品。"
            "工厂也留空时表示全局回退规则。"
        ),
    )

    class Meta:
        model = PricePolicy

        fields = (
            "name",
            "factory",
            "category",
            "start_date",
            "end_date",
            "hospital_unit_price",
            "factory_unit_price",
            "expiration_threshold_days",
            "notes",
            "is_active",
        )

        labels = {
            "name": "规则名称",
            "factory": "适用工厂",
            "start_date": "开始日期",
            "end_date": "结束日期",
            "hospital_unit_price": "医院销售单价",
            "factory_unit_price": "工厂采购单价",
            "expiration_threshold_days": "临期门槛天数",
            "notes": "备注",
            "is_active": "启用",
        }

        help_texts = {
            "name": (
                "建议包含工厂、分类和年份，"
                "例如：SINOMED 支架 2026。"
            ),
            "factory": (
                "留空表示全局回退规则。"
            ),
            "start_date": (
                "包含该日期。留空表示无下限。"
            ),
            "end_date": (
                "包含该日期。留空表示长期有效。"
            ),
            "expiration_threshold_days": (
                "例如 365 表示距离有效期不足 "
                "365 天时应用临期折扣。"
            ),
        }

        widgets = {
            "start_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "end_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "notes": forms.Textarea(
                attrs={"rows": 5}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        current_factory_id = (
            self.instance.factory_id
            if self.instance.pk
            else None
        )

        current_category_id = (
            self.instance.category_id
            if self.instance.pk
            else None
        )

        factory_filter = Q(is_active=True)

        if current_factory_id:
            factory_filter |= Q(
                id=current_factory_id
            )

        self.fields["factory"].queryset = (
            Factory.objects.filter(
                factory_filter
            )
            .distinct()
            .order_by(
                "short_name",
                "name",
            )
        )

        category_filter = Q(
            node_type=(
                ProductCategory.NodeType.CATEGORY
            ),
            is_active=True,
        )

        if current_category_id:
            category_filter |= Q(
                id=current_category_id
            )

        self.fields["category"].queryset = (
            ProductCategory.objects.filter(
                category_filter
            )
            .select_related(
                "parent",
                "parent__parent",
                "parent__factory",
            )
            .distinct()
            .order_by(
                "parent__parent__name",
                "parent__name",
                "name",
            )
        )

        for field in self.fields.values():
            widget = field.widget

            if isinstance(
                widget,
                forms.CheckboxInput,
            ):
                widget.attrs.setdefault(
                    "class",
                    "master-checkbox",
                )
            else:
                widget.attrs.setdefault(
                    "class",
                    "master-form-control",
                )

        if self.instance.pk:
            rate = (
                self.instance.expiration_discount_rate
                or Decimal("0")
            )

            self.initial[
                "expiration_discount_percent"
            ] = (
                rate * Decimal("100")
            )

        elif not self.is_bound:
            self.fields["is_active"].initial = True
            self.fields[
                "expiration_threshold_days"
            ].initial = 365

        self.order_fields(
            [
                "name",
                "factory",
                "category",
                "start_date",
                "end_date",
                "hospital_unit_price",
                "factory_unit_price",
                "expiration_threshold_days",
                "expiration_discount_percent",
                "notes",
                "is_active",
            ]
        )

    def clean_name(self):
        return (
            self.cleaned_data.get("name")
            or ""
        ).strip()

    def clean_expiration_discount_percent(self):
        value = self.cleaned_data[
            "expiration_discount_percent"
        ]

        if value < 0 or value > 100:
            raise forms.ValidationError(
                "折扣率必须在 0% 到 100% 之间。"
            )

        return value

    def clean(self):
        cleaned = super().clean()

        percent = cleaned.get(
            "expiration_discount_percent"
        )

        if percent is not None:
            self.instance.expiration_discount_rate = (
                percent / Decimal("100")
            )

        return cleaned

    def save(self, commit=True):
        policy = super().save(commit=False)

        percent = self.cleaned_data[
            "expiration_discount_percent"
        ]

        policy.expiration_discount_rate = (
            percent / Decimal("100")
        )

        if commit:
            policy.save()

        return policy


class PricePolicySimulationForm(forms.Form):
    product = ProductChoiceField(
        label="产品编号",
        queryset=Product.objects.none(),
        empty_label="请选择产品",
    )

    target_date = forms.DateField(
        label="医院订单日期",
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )

    def __init__(self, *args, **kwargs):
        initial_date = kwargs.pop(
            "initial_date",
            None,
        )

        super().__init__(*args, **kwargs)

        self.fields["product"].queryset = (
            Product.objects.filter(
                is_active=True,
            )
            .select_related(
                "factory",
                "category",
            )
            .order_by("code")
        )

        if (
            initial_date
            and not self.is_bound
        ):
            self.fields[
                "target_date"
            ].initial = initial_date

        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "master-form-control",
            )
