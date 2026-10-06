from django.utils.translation import gettext as _
from django.urls import reverse
from hospital_engagements.models import ACCESS_PERMISSION, HospitalEngagement
from hospital_engagements.boss_access import is_boss_user
from portal.services.common import (
    get_portal_lang,
    get_user_display_name,
    safe_count,
)


def build_home_context(request):
    lang = get_portal_lang(request)

    counters = {
        "orders": safe_count("orders", "Order"),
        "factory_confirmations": safe_count("factory_confirmations", "FactoryConfirmation"),
        "workflow": safe_count("workflow", "DocumentWorkflowItem"),
        "documents": safe_count("documents", "GeneratedDocument"),
        "products": safe_count("products", "Product"),
        "hospitals": safe_count("hospitals", "Hospital"),
        "factories": safe_count("factories", "Factory"),
        "settlements": safe_count(
            "settlements",
            "SettlementAccount",
        ),
    }

    modules = [
        {
            "title": _("资料库"),
            "subtitle": _("产品、医院、工厂与价格等基础资料"),
            "description": _("维护系统运行所需的主数据"),
            "icon": "portal/img/app-icons/library.png",
            "theme": "purple",
            "url": "/portal/library/",
            "badge": None,
            "status_text": _("基础资料"),
        },
        {
            "title": _("医院订单"),
            "subtitle": _("上传、提取与查看医院订单"),
            "description": _("处理医院采购订单与产品明细"),
            "icon": "portal/img/app-icons/hospital-orders.png",
            "theme": "teal",
            "url": "/portal/orders/",
            "badge": counters.get("orders"),
            "status_text": _("订单"),
        },
        {
            "title": _("工厂采购"),
            "subtitle": _("工厂采购、序列号、有效期与发货信息"),
            "description": _("管理工厂采购文件和采购相关流程"),
            "icon": "portal/img/app-icons/factory-purchase.png",
            "theme": "green",
            "url": "/portal/factory/",
            "badge": counters.get("factory_confirmations"),
            "status_text": _("确认文件"),
        },
        {
            "title": _("工作流"),
            "subtitle": _("跟踪订单状态、待处理事项与文件生成"),
            "description": _("集中处理订单从确认到出单的完整流程"),
            "icon": "portal/img/app-icons/workflow.png",
            "theme": "violet",
            "url": "/portal/workflow/",
            "badge": counters.get("workflow"),
            "status_text": _("流程项"),
        },
        {
            "title": _("文档中心"),
            "subtitle": _("查看和下载所有业务文件"),
            "description": _("集中管理 Factory Request、PO、Invoice 等文件"),
            "icon": "portal/img/app-icons/documents.png",
            "theme": "blue",
            "url": "/portal/documents/",
            "badge": counters.get("documents"),
            "status_text": _("文档"),
        },
        {
            "title": _("财务数据"),
            "subtitle": _("销售额、成本、利润与经营数据"),
            "description": _("查看财务表现和业务趋势"),
            "icon": "portal/img/app-icons/finance.png",
            "theme": "orange",
            "url": "/portal/finance/",
            "badge": None,
            "status_text": _("分析"),
        },
        {
            "title": _("发票与结算"),
            "subtitle": _("医院应收、工厂应付与收付款流水"),
            "description": _("管理结算状态、到期日期和收付款记录"),
            "icon": "portal/img/app-icons/invoice.png",
            "theme": "blue",
            "url": "/portal/settlements/",
            "badge": counters.get("settlements"),
            "status_text": _("结算"),
        },
    ]

    if request.user.has_perm(ACCESS_PERMISSION):
        modules.insert(1, {
            "title": _("医院沟通进度"),
            "subtitle": _("管理合作医院的沟通阶段、联系人、产品需求和下一步行动。"),
            "description": _("医院商务跟进"),
            "icon": "portal/img/app-icons/hospital-orders.png",
            "theme": "teal",
            "url": reverse("portal:engagement_home"),
            "badge": HospitalEngagement.objects.filter(hospital__is_active=True).count(),
            "status_text": _("医院"),
        })

    if is_boss_user(request.user):
        modules.append({
            "title": _("团队工作概览"),
            "subtitle": _("每日沟通、阶段推进与待跟进情况"),
            "description": _("查看业务团队的医院跟进工作"),
            "icon": "portal/img/app-icons/workflow.png",
            "theme": "violet",
            "url": reverse("portal:team_activity"),
            "badge": None,
            "status_text": _("团队工作概览"),
        })

    return {
        "modules": modules,
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
    }
