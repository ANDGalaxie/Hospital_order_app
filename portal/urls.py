from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),

    path("workflow/", views.workflow_list, name="workflow_list"),
    path("workflow/<int:item_id>/", views.workflow_detail, name="workflow_detail"),
    path(
        "workflow/<int:item_id>/action/",
        views.workflow_item_action,
        name="workflow_item_action",
    ),

    path("orders/", views.order_list, name="order_list"),
    path("orders/upload/", views.order_upload, name="order_upload"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("orders/<int:order_id>/edit/", views.order_edit, name="order_edit"),
    path("orders/<int:order_id>/action/", views.order_action, name="order_action"),

    path("factory/", views.factory_list, name="factory_list"),
    path("factory/upload/", views.factory_upload, name="factory_upload"),
    path("factory/<int:confirmation_id>/", views.factory_detail, name="factory_detail"),
    path("factory/<int:confirmation_id>/action/", views.factory_action, name="factory_action"),

    path("library/", views.library_home, name="library_home"),
    path("library/products/", views.library_products, name="library_products"),
    path("library/hospitals/", views.library_hospitals, name="library_hospitals"),
    path("library/factories/", views.library_factories, name="library_factories"),
    path("library/prices/", views.library_prices, name="library_prices"),
]