from django.urls import path
from . import views

app_name = "administrative_expenses"
urlpatterns = [
    path("", views.home, name="home"),
    path("new/", views.edit, name="create"),
    path("category/<slug:category>/", views.category, name="category"),
    path("category/<slug:category>/<slug:subcategory>/", views.expense_list, name="list"),
    path("<int:expense_id>/", views.detail, name="detail"),
    path("<int:expense_id>/edit/", views.edit, name="edit"),
    path("<int:expense_id>/void/", views.void, name="void"),
    path("<int:expense_id>/attachments/", views.upload, name="upload"),
    path("<int:expense_id>/attachments/<int:attachment_id>/download/", views.download, name="download"),
    path("<int:expense_id>/attachments/<int:attachment_id>/view/", views.download, {"preview": True}, name="preview"),
    path("<int:expense_id>/attachments/<int:attachment_id>/remove/", views.remove, name="remove"),
]
