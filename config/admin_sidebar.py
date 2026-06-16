from types import MethodType

from django.contrib import admin


def custom_get_app_list(self, request, app_label=None):
    """
    自定义 Django Admin 左侧栏分组和顺序。

    目标顺序：
        1. 库
        2. Orders
        3. Factory confirmations
        4. Documents
    """

    # 先拿到 Django 默认生成的 app list
    original_app_list = self._original_get_app_list(request, app_label)

    # 把所有 model 展平成一个 map，方便重新分组
    model_map = {}

    for app in original_app_list:
        for model in app["models"]:
            model_key = model["model"]._meta.label_lower
            model_map[model_key] = model

    # 你希望的分组定义
    group_definitions = [
        {
            "name": "库",
            "app_label": "library",
            "models": [
                "hospitals.hospital",
                "products.productbrowsercategory",
                "factories.factory",
                "shipments.shipmentmonth",
                "backorders.backorderrootfolder",
                "pricing.pricepolicy",
            ],
        },
        {
            "name": "Orders",
            "app_label": "orders_group",
            "models": [
                "orders.order",
            ],
        },
        {
            "name": "Factory confirmations",
            "app_label": "factory_confirmations_group",
            "models": [
                "factory_confirmations.factoryconfirmation",
                "factory_confirmations.serialitem",
            ],
        },
        {
            "name": "Documents",
            "app_label": "documents_group",
            "models": [
                "documents.documentsequence",
                "documents.factoryorderrequestdocument",
                "documents.hospitalinvoicedocument",
                "documents.factorypurchaseorderdocument",
            ],
        },
        {
            "name": "财务分析",
            "app_label": "finance_group",
            "models": [
                "finance.financedashboard",
            ],
        },
    ]

    used_model_keys = set()
    custom_app_list = []

    # 按照我们的分组定义重建左侧栏
    for group in group_definitions:
        models = []

        for model_key in group["models"]:
            if model_key in model_map:
                models.append(model_map[model_key])
                used_model_keys.add(model_key)

        if models:
            custom_app_list.append(
                {
                    "name": group["name"],
                    "app_label": group["app_label"],
                    "app_url": "",
                    "has_module_perms": True,
                    "models": models,
                }
            )

    # 如果还有没被分组进去的 model，保留在最后，避免“消失”
    for app in original_app_list:
        remaining_models = []

        for model in app["models"]:
            model_key = model["model"]._meta.label_lower
            if model_key not in used_model_keys:
                remaining_models.append(model)

        if remaining_models:
            custom_app_list.append(
                {
                    "name": app["name"],
                    "app_label": app["app_label"],
                    "app_url": app.get("app_url", ""),
                    "has_module_perms": app.get("has_module_perms", True),
                    "models": remaining_models,
                }
            )

    return custom_app_list


def patch_admin_sidebar():
    """
    给默认 admin.site 打补丁，只 patch 一次。
    """
    if getattr(admin.site, "_custom_sidebar_patched", False):
        return

    admin.site._original_get_app_list = admin.site.get_app_list
    admin.site.get_app_list = MethodType(custom_get_app_list, admin.site)
    admin.site._custom_sidebar_patched = True
