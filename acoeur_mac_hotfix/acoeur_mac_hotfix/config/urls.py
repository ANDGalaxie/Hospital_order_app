from django.conf.urls.static import static
from django.shortcuts import redirect
from django.conf import settings
from django.contrib import admin
from django.urls import path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
]

urlpatterns += [
    re_path(
        r"^media/(?P<path>.*)$",
        serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]

