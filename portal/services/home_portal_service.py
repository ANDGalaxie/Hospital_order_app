from portal.services.common import (
    get_portal_lang,
    get_user_display_name,
    safe_count,
)
from backorders.models import BackorderLine
from shipments.models import ShipmentBatch


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
        "shipment_batches": ShipmentBatch.objects.count(),
        "active_backorders": BackorderLine.objects.filter(
            is_active=True,
            remaining_quantity__gt=0,
        ).count(),
    }

    modules = [
        {
            "title": "资料库",
            "subtitle": "产品、医院、工厂与价格等基础资料",
            "description": "维护系统运行所需的主数据",
            "icon": "portal/img/app-icons/library.png",
            "theme": "purple",
            "url": "/portal/library/",
            "badge": None,
            "status_text": "基础资料",
        },
        {
            "title": "医院订单",
            "subtitle": "上传、提取与查看医院订单",
            "description": "处理医院采购订单与产品明细",
            "icon": "portal/img/app-icons/hospital-orders.png",
            "theme": "teal",
            "url": "/portal/orders/",
            "badge": counters.get("orders"),
            "status_text": "订单",
        },
        {
            "title": "工厂采购",
            "subtitle": "工厂采购、序列号、有效期与发货信息",
            "description": "管理工厂采购文件和采购相关流程",
            "icon": "portal/img/app-icons/factory-purchase.png",
            "theme": "green",
            "url": "/portal/factory/",
            "badge": counters.get("factory_confirmations"),
            "status_text": "确认文件",
        },
        {
            "title": "工作流",
            "subtitle": "跟踪订单状态、待处理事项与文件生成",
            "description": "集中处理订单从确认到出单的完整流程",
            "icon": "portal/img/app-icons/workflow.png",
            "theme": "violet",
            "url": "/portal/workflow/",
            "badge": counters.get("workflow"),
            "status_text": "流程项",
        },
        {
            "title": "Shipment Batches",
            "subtitle": "查看发货批次、数量快照与工作流状态",
            "description": "集中查看每一批已发货记录",
            "icon": "portal/img/app-icons/workflow.png",
            "theme": "violet",
            "url": "/portal/shipments/",
            "badge": counters.get("shipment_batches"),
            "status_text": "发货批次",
        },
        {
            "title": "Backorder Library",
            "subtitle": "查看待发产品并进入工厂补发或库存补发",
            "description": "集中处理当前 active backorder lines",
            "icon": "portal/img/app-icons/library.png",
            "theme": "teal",
            "url": "/portal/backorders/",
            "badge": counters.get("active_backorders"),
            "status_text": "待发行",
        },
        {
            "title": "文档中心",
            "subtitle": "查看和下载所有业务文件",
            "description": "集中管理 Factory Request、PO、Invoice 等文件",
            "icon": "portal/img/app-icons/documents.png",
            "theme": "blue",
            "url": "/portal/documents/",
            "badge": counters.get("documents"),
            "status_text": "文档",
        },
        {
            "title": "财务数据",
            "subtitle": "销售额、成本、利润与经营数据",
            "description": "查看财务表现和业务趋势",
            "icon": "portal/img/app-icons/finance.png",
            "theme": "orange",
            "url": "/portal/finance/",
            "badge": None,
            "status_text": "分析",
        },
        {
            "title": "发票与结算",
            "subtitle": "医院应收、工厂应付与收付款流水",
            "description": "管理结算状态、到期日期和收付款记录",
            "icon": "portal/img/app-icons/invoice.png",
            "theme": "blue",
            "url": "/portal/settlements/",
            "badge": counters.get("settlements"),
            "status_text": "结算",
        },
    ]

    return {
        "modules": modules,
        "lang": lang,
        "user_display_name": get_user_display_name(request.user),
    }
