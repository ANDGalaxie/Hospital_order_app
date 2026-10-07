from pathlib import Path

from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from commercial_pos.services.price_service import CommercialPOError
from commercial_pos.services.snapshot_service import validated_snapshot
from documents.models import GeneratedDocument
from portal.commercial_access import document_matches_batch, showcase_required
from portal.services.commercial_showcase_service import (
    build_commercial_finance_context, build_showcase_batch_context, build_showcase_list_context,
)


@method_decorator(never_cache, name="dispatch")
class ShowcaseLoginView(LoginView):
    template_name = "portal/commercial/login.html"

    def get_success_url(self):
        return reverse("portal:home")


class ShowcaseLogoutView(LogoutView):
    next_page = "/portal/login/"

    def get_success_url(self):
        return reverse("portal:showcase_login")


@showcase_required
@require_safe
def purchase_orders(request):
    return render(request, "portal/commercial/purchase_list.html", build_showcase_list_context(request))


@showcase_required
@require_safe
def operations(request):
    return render(request, "portal/commercial/operations_list.html", build_showcase_list_context(request, operations=True))


@showcase_required
@require_safe
def finance(request):
    return render(request, "portal/commercial/finance.html", build_commercial_finance_context(request))


@showcase_required
@require_safe
def batch_detail(request, batch_id):
    context = build_showcase_batch_context(request, batch_id)
    return render(request, "portal/commercial/operations_detail.html" if context["operations"] else "portal/commercial/purchase_detail.html", context)


@showcase_required
@require_safe
def purchase_detail(request, batch_id):
    return render(request, "portal/commercial/purchase_detail.html", build_showcase_batch_context(request, batch_id, operations=False))


@showcase_required
@require_safe
def operations_detail(request, batch_id):
    return render(request, "portal/commercial/operations_detail.html", build_showcase_batch_context(request, batch_id, operations=True))


@showcase_required
@require_safe
def document_file(request, document_id, kind):
    if kind not in {"pdf", "html"}:
        raise Http404("File not found")
    document = get_object_or_404(
        GeneratedDocument.objects.select_related("shipment_batch"), pk=document_id,
        document_type__in=["hospital_invoice", "commercial_po"], shipment_batch__isnull=False,
    )
    if not document_matches_batch(document):
        raise Http404("File not found")
    if document.document_type == "commercial_po":
        try:
            validated_snapshot(document)
        except CommercialPOError:
            raise Http404("File not found")
    field = getattr(document, f"{kind}_file")
    if not field:
        raise Http404("File not found")
    root = Path(settings.MEDIA_ROOT).resolve()
    try:
        path = Path(field.path).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise ValueError
        handle = path.open("rb")
    except (OSError, ValueError, NotImplementedError, SuspiciousFileOperation):
        raise Http404("File not found")
    response = FileResponse(handle, content_type="application/pdf" if kind == "pdf" else "text/html; charset=utf-8",
                            as_attachment=request.GET.get("download") == "1", filename=f"{document.document_number}.{kind}")
    response["Cache-Control"] = "private, no-store"
    response["Vary"] = "Cookie"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    return response
