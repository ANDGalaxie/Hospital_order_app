from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class CommercialPOPricePolicy(models.Model):
    """Independent, inclusive commercial display price intervals."""

    name = models.CharField(max_length=200)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    currency = models.CharField(max_length=3, default="EUR", choices=[("EUR", "EUR")])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "id"]
        permissions = [("view_commercial_showcase", "Can view commercial showcase")]
        constraints = [
            models.CheckConstraint(condition=Q(currency="EUR"), name="commercial_policy_eur"),
            models.CheckConstraint(condition=Q(unit_price__gt=0), name="commercial_policy_positive"),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(end_date__isnull=True)
                | Q(start_date__lte=models.F("end_date")),
                name="commercial_policy_valid_interval",
            ),
        ]

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("Start date must not follow end date.")
        if self.is_active:
            overlaps = type(self).objects.filter(is_active=True).exclude(pk=self.pk)
            if self.start_date:
                overlaps = overlaps.filter(Q(end_date__isnull=True) | Q(end_date__gte=self.start_date))
            if self.end_date:
                overlaps = overlaps.filter(Q(start_date__isnull=True) | Q(start_date__lte=self.end_date))
            if overlaps.exists():
                raise ValidationError("Active commercial price intervals must not overlap.")

    def __str__(self):
        return self.name
