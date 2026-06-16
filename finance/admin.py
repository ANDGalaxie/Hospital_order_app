from django.contrib import admin
from django.template.response import TemplateResponse

from finance.models import FinanceDashboard
from finance.services.finance_analysis_service import build_finance_dashboard_data


@admin.register(FinanceDashboard)
class FinanceDashboardAdmin(admin.ModelAdmin):
    """
    财务分析入口。
    点击左边栏“财务分析”后，直接显示中文仪表盘。
    """

    def changelist_view(self, request, extra_context=None):
        dashboard_data = build_finance_dashboard_data()

        context = {
            **self.admin_site.each_context(request),
            "title": "财务分析仪表盘",
            "kpi_cards": dashboard_data["kpi_cards"],
            "chart_data": dashboard_data["chart_data"],
            "top_products": dashboard_data["top_products"],
        }

        return TemplateResponse(
            request,
            "admin/finance/dashboard.html",
            context,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False