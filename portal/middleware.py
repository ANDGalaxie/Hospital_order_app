from django.conf import settings


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
