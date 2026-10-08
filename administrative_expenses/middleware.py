from django.http import FileResponse, HttpResponseForbidden
from hospital_engagements.boss_access import is_boss_user
from .storage import is_private_media_path


class PrivateExpenseResponseMiddleware:
    """A final boundary also covers old file endpoints pointing at private aliases."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if isinstance(response, FileResponse):
            stream = response.file_to_stream
            name = getattr(stream, "name", "")
            match = getattr(request, "resolver_match", None)
            dedicated = bool(match and match.view_name in {
                "portal:administrative_expenses:download", "portal:administrative_expenses:preview",
            } and is_boss_user(request.user))
            if isinstance(name, (str, bytes)) and name and is_private_media_path(name) and not dedicated:
                stream.close()
                return HttpResponseForbidden("Access denied")
        return response
