from django import forms

from .models import Order


class OrderCreateForm(forms.ModelForm):
    """
    创建医院订单的表单。

    Milestone 1 阶段：
    - 手动输入 bon de commande
    - 手动输入医院名称
    - 上传医院订单 PDF
    - 可填写备注

    后续 Milestone 2：
    - 医院名称可以由 OCR 自动提取
    - 产品和地址可以由 OCR 自动提取
    """

    class Meta:
        model = Order
        fields = [
            "bon_de_commande",
            "hospital_name",
            "hospital_order_pdf",
            "notes",
        ]

        widgets = {
            "bon_de_commande": forms.TextInput(
                attrs={
                    "placeholder": "Example: 150222",
                }
            ),
            "hospital_name": forms.TextInput(
                attrs={
                    "placeholder": "Example: CLINIQUE LOUIS PASTEUR",
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Optional notes",
                }
            ),
        }
