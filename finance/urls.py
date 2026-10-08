from django.urls import path

from finance import views


app_name = "finance"


urlpatterns = [
    path("operations/", views.operating_dashboard, name="operating_dashboard"),
    path(
        "",
        views.settlement_dashboard,
        name="settlement_dashboard",
    ),
    path(
        "export.xlsx",
        views.settlement_dashboard_export,
        name="settlement_dashboard_export",
    ),
]
