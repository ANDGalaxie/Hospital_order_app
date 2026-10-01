from django.http import JsonResponse


def healthz(request):
    """Process-level health check; deliberately performs no database writes."""
    return JsonResponse({"ok": True, "service": "acoeurs-orders"})
