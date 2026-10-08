from django.urls import re_path
from config.urls import urlpatterns as existing
from portal.media_views import protected_media

urlpatterns = list(existing) + [re_path(r"^media/(?P<path>.*)$", protected_media)]
