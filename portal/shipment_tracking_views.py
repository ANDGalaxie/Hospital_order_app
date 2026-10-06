from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from portal.forms.shipment_tracking_forms import ShipmentTrackingNumberForm
from portal.services.common import get_safe_next_url
from shipments.models import ShipmentBatch


@staff_member_required
@require_http_methods(["GET", "POST"])
def edit(request, batch_id):
    batch = get_object_or_404(
        ShipmentBatch.objects.select_related("order__hospital"), pk=batch_id,
    )
    back_url = get_safe_next_url(request, reverse("portal:factory_list"))
    form = ShipmentTrackingNumberForm(
        request.POST if request.method == "POST" else None, instance=batch,
    )
    title = _("修改快递单号") if batch.tracking_number else _("填写快递单号")
    if request.method == "POST" and form.is_valid():
        # Update this manual field alone; no status, history, or sync side effects.
        form.save(commit=False).save(update_fields=["tracking_number"])
        messages.success(request, _("快递单号已保存。"))
        return redirect(back_url)
    return render(request, "portal/shipments/tracking_edit.html", {
        "batch": batch, "form": form, "back_url": back_url, "title": title,
    }, status=400 if request.method == "POST" else 200)

