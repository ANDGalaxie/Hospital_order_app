from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from portal.media_views import protected_media

from .views import healthz

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("i18n/", include("django.conf.urls.i18n")),
    path("admin/", admin.site.urls),
    path("portal/", include("portal.urls")),
]

if settings.DEBUG:
    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            protected_media,
        ),
    ]
