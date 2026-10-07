from django.conf import settings

from django.http import HttpResponseForbidden
from django.utils.cache import patch_vary_headers

from portal.commercial_access import DEMO_POST_ROUTES, DEMO_ROUTES, SAFE_METHODS, is_demo


class PortalDefaultLanguageMiddleware:
    """Default Portal to Chinese until a Django language cookie is chosen.

    LocaleMiddleware remains responsible for activation and persistence uses
    only Django's standard set_language cookie. Browser preferences and the
    obsolete portal_lang session key are intentionally not Portal preferences.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        original = request.META.get("HTTP_ACCEPT_LANGUAGE")
        # LocaleMiddleware gives a valid language cookie priority over this
        # default header, including when an obsolete/invalid cookie is present.
        if request.path_info.startswith("/portal/"):
            request.META["HTTP_ACCEPT_LANGUAGE"] = settings.LANGUAGE_CODE
        try:
            return self.get_response(request)
        finally:
            if original is None:
                request.META.pop("HTTP_ACCEPT_LANGUAGE", None)
            else:
                request.META["HTTP_ACCEPT_LANGUAGE"] = original


class DemoIsolationMiddleware:
    """Fail closed across every Django route, including Admin and future APIs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        from portal.role_access import hospital_browser
        if (is_demo(request.user) or hospital_browser(request.user)) and response.status_code >= 400:
            # DEBUG error pages can include paths/settings and are never display-safe.
            from django.http import HttpResponse
            from django.utils.translation import gettext as _
            response = HttpResponse(_("此资源暂不可用。"), status=response.status_code, content_type="text/plain; charset=utf-8")
        if request.user.is_authenticated or request.path_info.startswith(("/portal/", "/media/", "/outputs/")):
            response["Cache-Control"] = "private, no-store"
            patch_vary_headers(response, ("Cookie",))
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not is_demo(request.user):
            from portal.role_access import role_route_allowed
            if not role_route_allowed(request.user, request.resolver_match.view_name, request.method):
                return HttpResponseForbidden("Access denied")
            return None
        route = request.resolver_match.view_name
        if (not request.user.is_active or route not in DEMO_ROUTES
                or (request.method not in SAFE_METHODS and not (request.method == "POST" and route in DEMO_POST_ROUTES))):
            return HttpResponseForbidden("Access denied")
        return None
