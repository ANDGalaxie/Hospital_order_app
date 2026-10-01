from portal import settlement_views
from django.urls import include, path

from . import media_views, views

app_name = "portal"

urlpatterns = [
    path("files/<path:path>", media_views.protected_media, name="protected_media"),
    path("finance/", include("finance.urls")),
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
    path(
        "orders/<int:order_id>/factory-upload/",
        views.order_factory_upload,
        name="order_factory_upload",
    ),
    path("orders/<int:order_id>/edit/", views.order_edit, name="order_edit"),
    path("orders/<int:order_id>/action/", views.order_action, name="order_action"),

    path("factory/", views.factory_list, name="factory_list"),
    path("factory/upload/", views.factory_upload, name="factory_upload"),
    path("factory/<int:confirmation_id>/", views.factory_detail, name="factory_detail"),
    path("factory/<int:confirmation_id>/action/", views.factory_action, name="factory_action"),
    path("shipments/", views.shipment_list, name="shipment_list"),
    path("shipments/<int:batch_id>/", views.shipment_detail, name="shipment_detail"),
    path("backorders/", views.backorder_list, name="backorder_list"),
    path(
        "backorders/export.xlsx",
        views.backorder_export_xlsx,
        name="backorder_export_xlsx",
    ),
    path("backorders/<int:backorder_id>/", views.backorder_detail, name="backorder_detail"),

    path("library/", views.library_home, name="library_home"),
    path(
        "library/products/",
        views.library_products,
        name="library_products",
    ),
    path(
        "library/products/departments/<int:department_id>/",
        views.library_product_department,
        name="library_product_department",
    ),
    path(
        "library/products/factories/<int:factory_node_id>/",
        views.library_product_factory,
        name="library_product_factory",
    ),
    path(
        "library/products/categories/<int:category_id>/",
        views.library_product_category,
        name="library_product_category",
    ),
    path(
        "library/products/items/<int:product_id>/",
        views.library_product_detail,
        name="library_product_detail",
    ),
    path(
    "library/products/departments/add/",
    views.library_product_department_add,
    name="library_product_department_add",
    ),
    path(
        "library/products/departments/<int:department_id>/factories/add/",
        views.library_product_factory_add,
        name="library_product_factory_add",
    ),
    path(
        "library/products/factories/<int:factory_node_id>/categories/add/",
        views.library_product_category_add,
        name="library_product_category_add",
    ),
    path(
        "library/products/categories/<int:category_id>/items/add/",
        views.library_product_add,
        name="library_product_add",
    ),
    path(
        "library/hospitals/",
        views.library_hospitals,
        name="library_hospitals",
    ),
    path(
        "library/hospitals/add/",
        views.library_hospital_add,
        name="library_hospital_add",
    ),
    path(
        "library/hospitals/<int:hospital_id>/",
        views.library_hospital_detail,
        name="library_hospital_detail",
    ),
    path(
        "library/hospitals/<int:hospital_id>/edit/",
        views.library_hospital_edit,
        name="library_hospital_edit",
    ),
    path(
        "library/hospitals/<int:hospital_id>/toggle-active/",
        views.library_hospital_toggle_active,
        name="library_hospital_toggle_active",
    ),
    path(
        "library/factories/",
        views.library_factories,
        name="library_factories",
    ),
    path(
        "library/factories/add/",
        views.library_factory_add,
        name="library_factory_add",
    ),
    path(
        "library/factories/<int:factory_id>/",
        views.library_factory_detail,
        name="library_factory_detail",
    ),
    path(
        "library/factories/<int:factory_id>/edit/",
        views.library_factory_edit,
        name="library_factory_edit",
    ),
    path(
        "library/factories/<int:factory_id>/toggle-active/",
        views.library_factory_toggle_active,
        name="library_factory_toggle_active",
    ),
    path(
        "library/prices/",
        views.library_prices,
        name="library_prices",
    ),
    path(
        "library/prices/add/",
        views.library_price_policy_add,
        name="library_price_policy_add",
    ),
    path(
        "library/prices/simulator/",
        views.library_price_policy_simulator,
        name="library_price_policy_simulator",
    ),
    path(
        "library/prices/<int:policy_id>/",
        views.library_price_policy_detail,
        name="library_price_policy_detail",
    ),
    path(
        "library/prices/<int:policy_id>/edit/",
        views.library_price_policy_edit,
        name="library_price_policy_edit",
    ),
    path(
        "library/prices/<int:policy_id>/toggle-active/",
        views.library_price_policy_toggle_active,
        name="library_price_policy_toggle_active",
    ),
    path(
        "documents/",
        views.document_center,
        name="document_center",
    ),
    path(
        "documents/all/",
        views.document_list,
        name="document_list",
    ),
    path(
        "documents/invoices/",
        views.document_invoices,
        name="document_invoices",
    ),
    path(
        "documents/factory-pos/",
        views.document_factory_pos,
        name="document_factory_pos",
    ),
    path(
        "documents/factory-requests/",
        views.document_factory_requests,
        name="document_factory_requests",
    ),
    path(
        "documents/<int:document_id>/",
        views.document_detail,
        name="document_detail",
    ),

    path(
        "settlements/",
        views.settlement_home,
        name="settlement_home",
    ),
    path(
        "settlements/receivables/",
        views.settlement_receivables,
        name="settlement_receivables",
    ),
    path(
        "settlements/payables/",
        views.settlement_payables,
        name="settlement_payables",
    ),
    path(
        "settlements/transactions/",
        views.settlement_transactions,
        name="settlement_transactions",
    ),

    path(
        "settlements/<int:account_id>/",
        settlement_views.settlement_account_detail,
        name="settlement_account_detail",
    ),
    path(
        "settlements/<int:account_id>/payment/add/",
        settlement_views.settlement_record_payment,
        name="settlement_record_payment",
    ),
    path(
        "settlements/<int:account_id>/due-date/",
        settlement_views.settlement_update_due_date,
        name="settlement_update_due_date",
    ),
    path(
        "settlements/transactions/<int:transaction_id>/reverse/",
        settlement_views.settlement_reverse_transaction,
        name="settlement_reverse_transaction",
    ),

]
