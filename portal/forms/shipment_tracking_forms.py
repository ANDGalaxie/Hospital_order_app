from django import forms
from django.utils.translation import gettext_lazy as _

from shipments.models import ShipmentBatch


class ShipmentTrackingNumberForm(forms.ModelForm):
    class Meta:
        model = ShipmentBatch
        fields = ["tracking_number"]
        labels = {"tracking_number": _("快递单号")}
        widgets = {"tracking_number": forms.TextInput(attrs={"autocomplete": "off"})}

