from django.utils.translation import gettext as _
from django.apps import apps
from django.urls import reverse

from backorders.models import BackorderLine
from shipments.models import ShipmentBatch


def safe_count(app_label, model_name):
    """
    安全统计某个模型的数据数量。

    如果模型不存在、app 名写错、数据库还没有迁移，
    这里不会让资料库首页报错，只返回 None。
    """
    try:
        model = apps.get_model(app_label, model_name)
        return model.objects.count()
    except Exception:
        return None


def build_library_home_context(request):
    """
    构建资料库首页。

    注意：
    系统首页 home.html 使用的是 modules 数据结构，
    所以资料库首页也必须返回 modules，而不是 cards。
    """
    product_count = safe_count("products", "Product")
    hospital_count = safe_count("hospitals", "Hospital")
    factory_count = safe_count("factories", "Factory")
    price_count = safe_count("pricing", "PricePolicy")
    shipment_batch_count = ShipmentBatch.objects.count()
    active_backorder_count = BackorderLine.objects.filter(
        is_active=True,
        remaining_quantity__gt=0,
    ).count()

    modules = [
        {
            "title": _("产品库"),
            "subtitle": _("维护 BMA 产品编号、规格、描述和产品匹配信息。"),
            "icon": "portal/img/app-icons/library.png",
            "theme": "purple",
            "url": reverse("portal:library_products"),
            "badge": product_count,
        },
        {
            "title": _("医院库"),
            "subtitle": _("维护医院名称、收货地址、账单地址和匹配规则。"),
            "icon": "portal/img/app-icons/hospital-orders.png",
            "theme": "teal",
            "url": reverse("portal:library_hospitals"),
            "badge": hospital_count,
        },
        {
            "title": _("工厂库"),
            "subtitle": _("维护供应工厂名称、地址和采购相关信息。"),
            "icon": "portal/img/app-icons/factory-purchase.png",
            "theme": "green",
            "url": reverse("portal:library_factories"),
            "badge": factory_count,
        },
        {
            "title": _("价格规则"),
            "subtitle": _("维护医院销售价格、工厂采购价格和价格策略。"),
            "icon": "portal/img/app-icons/finance.png",
            "theme": "orange",
            "url": reverse("portal:library_prices"),
            "badge": price_count,
        },
        {
            "title": _("发货批次库"),
            "subtitle": _("查看每一批发货记录、数量快照与工作流状态。"),
            "icon": "portal/img/app-icons/workflow.png",
            "theme": "violet",
            "url": reverse("portal:shipment_list"),
            "badge": shipment_batch_count,
        },
        {
            "title": _("待补发库"),
            "subtitle": _("查看当前待补发行并进入工厂补发或库存补发。"),
            "icon": "portal/img/app-icons/library.png",
            "theme": "teal",
            "url": reverse("portal:backorder_list"),
            "badge": active_backorder_count,
        },
    ]

    return {
        "modules": modules,
    }
