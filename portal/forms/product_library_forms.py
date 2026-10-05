from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django import forms
from django.db.models import Q

from factories.models import Factory
from products.models import Product, ProductCategory


class PortalStyledFormMixin:
    """
    给 Portal 表单统一添加 CSS class。
    """

    def apply_portal_styles(self):
        for field in self.fields.values():
            field.help_text = gettext(str(field.help_text))
            widget = field.widget

            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault(
                    "class",
                    "library-form-checkbox",
                )
            elif isinstance(widget, forms.RadioSelect):
                widget.attrs.setdefault(
                    "class",
                    "library-form-radio",
                )
            else:
                widget.attrs.setdefault(
                    "class",
                    "library-form-control",
                )


class DepartmentCreateForm(
    PortalStyledFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = ProductCategory
        fields = (
            "name",
            "sort_order",
            "notes",
            "is_active",
        )
        labels = {
            "name": _("科室名称"),
            "sort_order": _("显示顺序"),
            "notes": _("备注"),
            "is_active": _("启用"),
        }
        widgets = {
            "notes": forms.Textarea(
                attrs={"rows": 4}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["is_active"].initial = True
        self.apply_portal_styles()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        exists = ProductCategory.objects.filter(
            parent__isnull=True,
            node_type=ProductCategory.NodeType.DEPARTMENT,
            name__iexact=name,
        ).exists()

        if exists:
            raise forms.ValidationError(
                _("已经存在同名科室。")
            )

        return name

    def save(self, commit=True):
        department = super().save(commit=False)

        department.parent = None
        department.factory = None
        department.node_type = (
            ProductCategory.NodeType.DEPARTMENT
        )

        if commit:
            department.save()

        return department


class FactoryNodeCreateForm(
    PortalStyledFormMixin,
    forms.Form,
):
    MODE_EXISTING = "existing"
    MODE_NEW = "new"

    mode = forms.ChoiceField(
        label=_("添加方式"),
        choices=(),
        widget=forms.RadioSelect,
    )

    existing_factory = forms.ModelChoiceField(
        label=_("选择已有工厂"),
        queryset=Factory.objects.none(),
        required=False,
        empty_label=_("请选择工厂"),
    )

    node_name = forms.CharField(
        label=_("卡片显示名称"),
        required=False,
        help_text=(
            _("可留空。留空时自动使用工厂简称或正式名称。")
        ),
    )

    new_factory_name = forms.CharField(
        label=_("新工厂正式名称"),
        required=False,
    )

    new_factory_legal_name = forms.CharField(
        label=_("新工厂法律名称"),
        required=False,
    )

    new_factory_short_name = forms.CharField(
        label=_("新工厂简称"),
        required=False,
    )

    new_factory_address = forms.CharField(
        label=_("新工厂地址"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    new_factory_buyer = forms.CharField(
        label=_("采购联系人 / Buyer"),
        required=False,
    )

    new_factory_match_keywords = forms.CharField(
        label=_("自动匹配关键词"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
        help_text=_("每行一个关键词。"),
    )

    sort_order = forms.IntegerField(
        label=_("显示顺序"),
        required=False,
        min_value=0,
        initial=0,
    )

    notes = forms.CharField(
        label=_("备注"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )

    is_active = forms.BooleanField(
        label=_("启用"),
        required=False,
        initial=True,
    )

    def __init__(
        self,
        *args,
        department,
        allow_create_factory=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.department = department
        self.allow_create_factory = allow_create_factory

        choices = [
            (
                self.MODE_EXISTING,
                _("关联已有工厂"),
            ),
        ]

        if allow_create_factory:
            choices.append(
                (
                    self.MODE_NEW,
                    _("创建新工厂"),
                )
            )

        self.fields["mode"].choices = choices
        self.fields["mode"].initial = self.MODE_EXISTING

        used_factory_ids = (
            ProductCategory.objects.filter(
                parent=department,
                node_type=ProductCategory.NodeType.FACTORY,
                factory__isnull=False,
            )
            .values_list("factory_id", flat=True)
        )

        self.fields["existing_factory"].queryset = (
            Factory.objects.filter(
                is_active=True,
            )
            .exclude(id__in=used_factory_ids)
            .order_by("name")
        )

        self.apply_portal_styles()

    def clean(self):
        cleaned = super().clean()

        mode = cleaned.get("mode")
        existing_factory = cleaned.get(
            "existing_factory"
        )

        if mode == self.MODE_EXISTING:
            if not existing_factory:
                self.add_error(
                    "existing_factory",
                    _("请选择一个已有工厂。"),
                )

        elif mode == self.MODE_NEW:
            if not self.allow_create_factory:
                raise forms.ValidationError(
                    _("当前用户没有创建工厂的权限。")
                )

            new_name = (
                cleaned.get("new_factory_name")
                or ""
            ).strip()

            new_short_name = (
                cleaned.get("new_factory_short_name")
                or ""
            ).strip()

            if not new_name:
                self.add_error(
                    "new_factory_name",
                    _("请输入新工厂正式名称。"),
                )

            duplicate_query = Q(
                name__iexact=new_name
            )

            if new_short_name:
                duplicate_query |= Q(
                    short_name__iexact=new_short_name
                )

            if new_name and Factory.objects.filter(
                duplicate_query
            ).exists():
                raise forms.ValidationError(
                    _("工厂库中似乎已经存在该工厂，"
                    "请改用“关联已有工厂”。")
                )

        return cleaned

    def save(self):
        mode = self.cleaned_data["mode"]

        if mode == self.MODE_EXISTING:
            factory = self.cleaned_data[
                "existing_factory"
            ]

        else:
            factory = Factory.objects.create(
                name=self.cleaned_data[
                    "new_factory_name"
                ].strip(),
                legal_name=(
                    self.cleaned_data.get(
                        "new_factory_legal_name"
                    )
                    or ""
                ).strip(),
                short_name=(
                    self.cleaned_data.get(
                        "new_factory_short_name"
                    )
                    or ""
                ).strip(),
                address=(
                    self.cleaned_data.get(
                        "new_factory_address"
                    )
                    or ""
                ).strip(),
                buyer=(
                    self.cleaned_data.get(
                        "new_factory_buyer"
                    )
                    or ""
                ).strip(),
                match_keywords=(
                    self.cleaned_data.get(
                        "new_factory_match_keywords"
                    )
                    or ""
                ).strip(),
                is_active=True,
            )

        node_name = (
            self.cleaned_data.get("node_name")
            or factory.short_name
            or factory.name
        ).strip()

        node = ProductCategory.objects.create(
            name=node_name,
            parent=self.department,
            node_type=ProductCategory.NodeType.FACTORY,
            factory=factory,
            sort_order=self.cleaned_data.get(
                "sort_order"
            ) or 0,
            notes=(
                self.cleaned_data.get("notes")
                or ""
            ).strip(),
            is_active=self.cleaned_data.get(
                "is_active",
                True,
            ),
        )

        return node


class ProductCategoryCreateForm(
    PortalStyledFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = ProductCategory
        fields = (
            "name",
            "sort_order",
            "notes",
            "is_active",
        )
        labels = {
            "name": _("产品分类名称"),
            "sort_order": _("显示顺序"),
            "notes": _("备注"),
            "is_active": _("启用"),
        }
        widgets = {
            "notes": forms.Textarea(
                attrs={"rows": 4}
            ),
        }

    def __init__(
        self,
        *args,
        factory_node,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.factory_node = factory_node
        self.fields["is_active"].initial = True
        self.apply_portal_styles()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        exists = ProductCategory.objects.filter(
            parent=self.factory_node,
            node_type=ProductCategory.NodeType.CATEGORY,
            name__iexact=name,
        ).exists()

        if exists:
            raise forms.ValidationError(
                _("该工厂下已经存在同名产品分类。")
            )

        return name

    def save(self, commit=True):
        category = super().save(commit=False)

        category.parent = self.factory_node
        category.factory = None
        category.node_type = (
            ProductCategory.NodeType.CATEGORY
        )

        if commit:
            category.save()

        return category


class ProductCreateForm(
    PortalStyledFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = Product
        fields = (
            "code",
            "description",
            "hospital_unit_price",
            "factory_unit_price",
            "notes",
            "is_active",
        )
        labels = {
            "code": _("产品编号"),
            "description": _("产品描述"),
            "hospital_unit_price": _("医院销售价格"),
            "factory_unit_price": _("工厂采购价格"),
            "notes": _("备注"),
            "is_active": _("启用"),
        }
        widgets = {
            "description": forms.Textarea(
                attrs={"rows": 4}
            ),
            "notes": forms.Textarea(
                attrs={"rows": 4}
            ),
        }

    def __init__(
        self,
        *args,
        category,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.category = category
        self.fields["is_active"].initial = True
        self.apply_portal_styles()

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()

        if Product.objects.filter(
            code__iexact=code
        ).exists():
            raise forms.ValidationError(
                _("该产品编号已经存在。")
            )

        return code

    def save(self, commit=True):
        product = super().save(commit=False)

        factory_node = self.category.parent

        if not factory_node or not factory_node.factory:
            raise ValueError(
                _("当前产品分类没有关联真实工厂，"
                "无法创建产品。")
            )

        product.category = self.category
        product.factory = factory_node.factory

        if commit:
            product.save()

        return product
